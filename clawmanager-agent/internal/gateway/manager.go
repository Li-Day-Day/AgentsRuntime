package gateway

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"
)

type gatewayRecord struct {
	state   GatewayState
	process ManagedProcess
}

type GatewayManager struct {
	cfg     Config
	starter ProcessStarter
	ports   *PortAllocator
	health  GatewayHealthChecker

	mu       sync.RWMutex
	draining bool
	gateways map[string]*gatewayRecord
}

func NewGatewayManager(cfg Config, starter ProcessStarter, ports *PortAllocator) *GatewayManager {
	var health GatewayHealthChecker = noopGatewayHealthChecker{}
	if starter == nil {
		starter = NewExecProcessStarter(cfg)
		if profileHealth := runtimeProfile(cfg).HealthChecker(cfg); profileHealth != nil {
			health = profileHealth
		} else {
			health = NewHTTPGatewayHealthChecker(cfg)
		}
	}
	if ports == nil {
		ports = NewPortAllocator(nil)
	}
	if cfg.GatewayPortBlockSize > 0 {
		ports.SetBlockSize(cfg.GatewayPortBlockSize)
	}
	return &GatewayManager{
		cfg:      cfg,
		starter:  starter,
		ports:    ports,
		health:   health,
		gateways: map[string]*gatewayRecord{},
	}
}

func (m *GatewayManager) SetHealthChecker(health GatewayHealthChecker) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if health == nil {
		health = noopGatewayHealthChecker{}
	}
	m.health = health
}

func (m *GatewayManager) CreateGateway(_ context.Context, req CreateGatewayRequest) (CreateGatewayResponse, error) {
	if strings.ToLower(strings.TrimSpace(req.AgentType)) != m.cfg.RuntimeType {
		return CreateGatewayResponse{}, ErrRuntimeType
	}
	workspacePath, err := ValidateWorkspacePath(m.cfg.WorkspaceRoot, m.cfg.RuntimeType, req)
	if err != nil {
		return CreateGatewayResponse{}, err
	}
	rng := req.PortRange
	if rng.Start == 0 && rng.End == 0 {
		rng = PortRange{Start: m.cfg.GatewayPortStart, End: m.cfg.GatewayPortEnd}
	}

	m.mu.Lock()

	if m.draining {
		m.mu.Unlock()
		return CreateGatewayResponse{}, ErrDraining
	}

	gatewayID := gatewayID(req.InstanceID, req.Generation)
	if existing, ok := m.gateways[gatewayID]; ok {
		resp := createGatewayResponse(existing.state)
		m.mu.Unlock()
		return resp, nil
	}

	var oldProcesses []ManagedProcess
	for id, record := range m.gateways {
		if record.state.InstanceID != req.InstanceID {
			continue
		}
		if record.state.Generation > req.Generation {
			m.mu.Unlock()
			return CreateGatewayResponse{}, ErrStaleGeneration
		}
		if record.state.Generation < req.Generation {
			oldProcesses = append(oldProcesses, m.detachGatewayLocked(id))
		}
	}

	capacity := m.effectiveCapacityLocked()
	if capacity <= 0 || m.usedSlotsLocked() >= capacity {
		m.mu.Unlock()
		for _, process := range oldProcesses {
			m.stopProcessAsync(process)
		}
		return CreateGatewayResponse{}, ErrNoFreePort
	}

	port, err := m.ports.Reserve(req.InstanceID, req.Generation, rng)
	if err != nil {
		m.mu.Unlock()
		for _, process := range oldProcesses {
			m.stopProcessAsync(process)
		}
		return CreateGatewayResponse{}, err
	}

	now := time.Now().UTC()
	state := GatewayState{
		InstanceID:    req.InstanceID,
		UserID:        req.UserID,
		GatewayID:     gatewayID,
		RuntimeType:   m.cfg.RuntimeType,
		WorkspacePath: workspacePath,
		Port:          port,
		PortAlias:     port,
		UID:           req.UID,
		GID:           req.GID,
		CPUCores:      req.CPUCores,
		MemoryMB:      req.MemoryMB,
		DiskQuotaMB:   req.DiskQuotaMB,
		Generation:    req.Generation,
		State:         "starting",
		StartedAt:     now,
		UpdatedAt:     now,
	}
	m.gateways[gatewayID] = &gatewayRecord{state: state}
	resp := createGatewayResponse(state)
	m.mu.Unlock()

	for _, process := range oldProcesses {
		m.stopProcessAsync(process)
	}
	go m.startGatewayInBackground(gatewayID, req, workspacePath, port)

	return resp, nil
}

func (m *GatewayManager) startGatewayInBackground(gatewayID string, req CreateGatewayRequest, workspacePath string, port int) {
	if err := m.profile().PrepareWorkspace(m.cfg, req, workspacePath); err != nil {
		m.markGatewayError(gatewayID, 0, err)
		return
	}
	if err := m.profile().WriteGatewayConfig(m.cfg, req, workspacePath); err != nil {
		m.markGatewayError(gatewayID, 0, err)
		return
	}

	spec := GatewayStartSpec{
		GatewayID:     gatewayID,
		RuntimeType:   m.cfg.RuntimeType,
		InstanceID:    req.InstanceID,
		UserID:        req.UserID,
		WorkspacePath: workspacePath,
		Port:          port,
		UID:           req.UID,
		GID:           req.GID,
		CPUCores:      req.CPUCores,
		MemoryMB:      req.MemoryMB,
		DiskQuotaMB:   req.DiskQuotaMB,
		Generation:    req.Generation,
		Command:       append([]string(nil), m.cfg.GatewayCommand...),
		Env:           m.profile().GatewayEnv(os.Environ(), m.cfg, req, workspacePath, port),
	}
	process, err := m.starter.StartGateway(context.Background(), spec)
	if err != nil {
		m.markGatewayError(gatewayID, 0, fmt.Errorf("%w: %v", ErrGatewayStartFailed, err))
		return
	}
	if !m.attachGatewayProcess(gatewayID, process) {
		m.stopProcessAsync(process)
		return
	}

	if err := m.health.WaitReady(context.Background(), spec); err != nil {
		m.stopProcessAsync(process)
		m.markGatewayError(gatewayID, process.PID, fmt.Errorf("%w: %v", ErrGatewayStartFailed, err))
		return
	}
	m.markGatewayRunning(gatewayID, req, process.PID)
	if process.Done != nil {
		go m.watchGatewayProcess(gatewayID, process.Done)
	}
}

func createGatewayResponse(state GatewayState) CreateGatewayResponse {
	var pid *int
	if state.PID > 0 {
		value := state.PID
		pid = &value
	}
	return CreateGatewayResponse{
		GatewayID:     state.GatewayID,
		InstanceID:    state.InstanceID,
		Port:          state.Port,
		PID:           pid,
		Status:        state.State,
		WorkspacePath: state.WorkspacePath,
	}
}

func (m *GatewayManager) DeleteGateway(ctx context.Context, gatewayID string) error {
	m.mu.Lock()
	process := m.detachGatewayLocked(gatewayID)
	m.mu.Unlock()
	if process.Stop != nil {
		_ = process.Stop(ctx)
	}
	return nil
}

func (m *GatewayManager) SetDraining(draining bool) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.draining = draining
}

func (m *GatewayManager) Draining() bool {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return m.draining
}

func (m *GatewayManager) UsedSlots() int {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return m.usedSlotsLocked()
}

func (m *GatewayManager) GatewayStates() []GatewayState {
	m.mu.RLock()
	defer m.mu.RUnlock()

	states := make([]GatewayState, 0, len(m.gateways))
	for _, record := range m.gateways {
		states = append(states, record.state)
	}
	return states
}

func (m *GatewayManager) Health() error {
	if m.cfg.WorkspaceRoot == "" {
		return fmt.Errorf("workspace root is empty")
	}
	if err := os.MkdirAll(m.cfg.WorkspaceRoot, 0o755); err != nil {
		return fmt.Errorf("workspace root unavailable: %w", err)
	}
	return nil
}

func (m *GatewayManager) profile() RuntimeProfile {
	return runtimeProfile(m.cfg)
}

func runtimeProfile(cfg Config) RuntimeProfile {
	if cfg.Runtime != nil {
		return cfg.Runtime
	}
	return openClawCompatProfile{}
}

func (m *GatewayManager) HeartbeatPayload(podID int) HeartbeatPayload {
	m.mu.RLock()
	defer m.mu.RUnlock()

	state := "ready"
	if m.draining {
		state = "draining"
	}
	usedSlots := m.usedSlotsLocked()
	maxGateways := m.effectiveCapacityLocked()
	return HeartbeatPayload{
		PodID:          podID,
		Namespace:      m.cfg.Namespace,
		PodName:        m.cfg.PodName,
		State:          state,
		MaxGateways:    maxGateways,
		UsedSlots:      usedSlots,
		AvailableSlots: maxInt(0, maxGateways-usedSlots),
		Draining:       m.draining,
		ReportedAt:     time.Now().UTC(),
	}
}

func (m *GatewayManager) RegisterPayload() RegisterPayload {
	m.mu.RLock()
	defer m.mu.RUnlock()

	state := "ready"
	if m.draining {
		state = "draining"
	}
	usedSlots := m.usedSlotsLocked()
	maxGateways := m.effectiveCapacityLocked()
	return RegisterPayload{
		RuntimeType:    m.cfg.RuntimeType,
		Namespace:      m.cfg.Namespace,
		PodName:        m.cfg.PodName,
		PodUID:         m.cfg.PodUID,
		PodIP:          m.cfg.PodIP,
		NodeName:       m.cfg.NodeName,
		DeploymentName: m.cfg.DeploymentName,
		ImageRef:       m.cfg.ImageRef,
		AgentEndpoint:  m.cfg.AgentEndpoint,
		State:          state,
		Capacity:       maxGateways,
		MaxGateways:    maxGateways,
		UsedSlots:      usedSlots,
		AvailableSlots: maxInt(0, maxGateways-usedSlots),
		Draining:       m.draining,
		ReportedAt:     time.Now().UTC(),
	}
}

func (m *GatewayManager) GatewayReportPayload(podID int) GatewayReportPayload {
	return GatewayReportPayload{
		PodID:     podID,
		Namespace: m.cfg.Namespace,
		PodName:   m.cfg.PodName,
		Gateways:  m.GatewayStates(),
	}
}

func (m *GatewayManager) stopGatewayLocked(ctx context.Context, id string) {
	process := m.detachGatewayLocked(id)
	if process.Stop != nil {
		_ = process.Stop(ctx)
	}
}

func (m *GatewayManager) detachGatewayLocked(id string) ManagedProcess {
	record, ok := m.gateways[id]
	if !ok {
		return ManagedProcess{}
	}
	m.ports.Release(record.state.Port)
	delete(m.gateways, id)
	return record.process
}

func (m *GatewayManager) attachGatewayProcess(id string, process ManagedProcess) bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	record, ok := m.gateways[id]
	if !ok {
		return false
	}
	now := time.Now().UTC()
	record.process = process
	record.state.PID = process.PID
	record.state.UpdatedAt = now
	return true
}

func (m *GatewayManager) markGatewayRunning(id string, req CreateGatewayRequest, pid int) {
	m.mu.Lock()
	defer m.mu.Unlock()
	record, ok := m.gateways[id]
	if !ok {
		return
	}
	now := time.Now().UTC()
	m.ports.Commit(req.InstanceID, req.Generation, record.state.Port)
	record.state.PID = pid
	record.state.State = "running"
	record.state.ErrorMessage = resourceLimitDegradation(req)
	record.state.HealthAt = now
	record.state.UpdatedAt = now
}

func (m *GatewayManager) markGatewayError(id string, pid int, cause error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	record, ok := m.gateways[id]
	if !ok {
		return
	}
	now := time.Now().UTC()
	m.ports.Release(record.state.Port)
	if pid > 0 {
		record.state.PID = pid
	}
	record.process = ManagedProcess{}
	record.state.State = "error"
	record.state.ErrorMessage = cause.Error()
	record.state.HealthAt = now
	record.state.UpdatedAt = now
}

func (m *GatewayManager) watchGatewayProcess(id string, done <-chan error) {
	err := <-done
	m.mu.Lock()
	defer m.mu.Unlock()
	record, ok := m.gateways[id]
	if !ok {
		return
	}
	now := time.Now().UTC()
	m.ports.Release(record.state.Port)
	record.process = ManagedProcess{}
	record.state.UpdatedAt = now
	record.state.HealthAt = now
	if err != nil {
		record.state.State = "error"
		record.state.ErrorMessage = err.Error()
		return
	}
	record.state.State = "stopped"
	record.state.ErrorMessage = ""
}

func (m *GatewayManager) stopProcessAsync(process ManagedProcess) {
	if process.Stop == nil {
		return
	}
	go func() {
		stopCtx, cancel := context.WithTimeout(context.Background(), m.stopTimeout())
		_ = process.Stop(stopCtx)
		cancel()
	}()
}

func (m *GatewayManager) usedSlotsLocked() int {
	count := 0
	for _, record := range m.gateways {
		switch record.state.State {
		case "running", "starting":
			count++
		}
	}
	return count
}

func (m *GatewayManager) effectiveCapacityLocked() int {
	portCapacity := portBlockCapacity(PortRange{Start: m.cfg.GatewayPortStart, End: m.cfg.GatewayPortEnd}, m.cfg.GatewayPortBlockSize)
	if m.cfg.Capacity <= 0 {
		return portCapacity
	}
	if portCapacity <= 0 {
		return 0
	}
	return minInt(m.cfg.Capacity, portCapacity)
}

func portBlockCapacity(rng PortRange, blockSize int) int {
	if rng.Start <= 0 || rng.End < rng.Start {
		return 0
	}
	if blockSize <= 0 {
		blockSize = 1
	}
	return (rng.End - rng.Start + 1) / blockSize
}

func gatewayID(instanceID, generation int) string {
	return "gw-" + strconv.Itoa(instanceID) + "-" + strconv.Itoa(generation)
}

func (m *GatewayManager) stopTimeout() time.Duration {
	if m.cfg.ProcessStopTimeout > 0 {
		return m.cfg.ProcessStopTimeout
	}
	return 20 * time.Second
}

func OpenClawGatewayEnv(base []string, cfg Config, req CreateGatewayRequest, workspacePath string, port int) []string {
	env := append([]string(nil), base...)
	env = ApplyRequestEnvironment(env, req)
	env = ApplyLiteTeamConfigEnvironment(env, req, workspacePath)
	env = setEnv(env, "CLAWMANAGER_INSTANCE_ID", strconv.Itoa(req.InstanceID))
	env = setEnv(env, "CLAWMANAGER_USER_ID", strconv.Itoa(req.UserID))
	env = setEnv(env, "CLAWMANAGER_RUNTIME_TYPE", cfg.RuntimeType)
	env = setEnv(env, "CLAWMANAGER_WORKSPACE_PATH", workspacePath)
	env = setEnv(env, "CLAWMANAGER_GATEWAY_PORT", strconv.Itoa(port))
	env = setEnv(env, "HOME", filepath.Join(workspacePath, "home"))
	env = setEnv(env, "HOST", "0.0.0.0")
	env = setEnv(env, "PORT", strconv.Itoa(port))
	env = setEnv(env, "OPENCLAW_HOST", "0.0.0.0")
	env = setEnv(env, "OPENCLAW_PORT", strconv.Itoa(port))
	env = setEnv(env, "OPENCLAW_GATEWAY_PORT", strconv.Itoa(port))
	if cfg.GatewayAuthMode == "trusted-proxy" {
		env = unsetEnv(env, "OPENCLAW_GATEWAY_TOKEN", "CLAWMANAGER_GATEWAY_TOKEN", "RUNTIME_GATEWAY_TOKEN")
	} else if cfg.GatewayToken != "" {
		env = setEnv(env, "OPENCLAW_GATEWAY_TOKEN", cfg.GatewayToken)
	}
	return env
}

func GenericGatewayEnv(base []string, cfg Config, req CreateGatewayRequest, workspacePath string, port int) []string {
	env := append([]string(nil), base...)
	env = ApplyRequestEnvironment(env, req)
	env = ApplyLiteTeamConfigEnvironment(env, req, workspacePath)
	env = setEnv(env, "CLAWMANAGER_INSTANCE_ID", strconv.Itoa(req.InstanceID))
	env = setEnv(env, "CLAWMANAGER_USER_ID", strconv.Itoa(req.UserID))
	env = setEnv(env, "CLAWMANAGER_RUNTIME_TYPE", cfg.RuntimeType)
	env = setEnv(env, "CLAWMANAGER_WORKSPACE_PATH", workspacePath)
	env = setEnv(env, "CLAWMANAGER_GATEWAY_PORT", strconv.Itoa(port))
	env = setEnv(env, "HOME", filepath.Join(workspacePath, "home"))
	env = setEnv(env, "HOST", "0.0.0.0")
	env = setEnv(env, "PORT", strconv.Itoa(port))
	if cfg.GatewayAuthMode == "trusted-proxy" {
		env = unsetEnv(env, "OPENCLAW_GATEWAY_TOKEN", "CLAWMANAGER_GATEWAY_TOKEN", "RUNTIME_GATEWAY_TOKEN")
	} else if cfg.GatewayToken != "" {
		env = setEnv(env, "RUNTIME_GATEWAY_TOKEN", cfg.GatewayToken)
	}
	return env
}

func setEnv(env []string, key, value string) []string {
	prefix := key + "="
	for i, item := range env {
		if strings.HasPrefix(item, prefix) {
			env[i] = prefix + value
			return env
		}
	}
	return append(env, prefix+value)
}

func unsetEnv(env []string, keys ...string) []string {
	remove := map[string]bool{}
	for _, key := range keys {
		remove[key+"="] = true
	}
	filtered := env[:0]
	for _, item := range env {
		keep := true
		for prefix := range remove {
			if strings.HasPrefix(item, prefix) {
				keep = false
				break
			}
		}
		if keep {
			filtered = append(filtered, item)
		}
	}
	return filtered
}

func resourceLimitDegradation(req CreateGatewayRequest) string {
	if req.CPUCores == 0 && req.MemoryMB == 0 && req.DiskQuotaMB == 0 {
		return ""
	}
	return "resource limit enforcement is degraded: cgroup CPU/memory and filesystem quota are not configured by this runtime-agent build"
}

func maxInt(left, right int) int {
	if left > right {
		return left
	}
	return right
}

func minInt(left, right int) int {
	if left < right {
		return left
	}
	return right
}

type ExecProcessStarter struct {
	cfg Config
}

func NewExecProcessStarter(cfg Config) *ExecProcessStarter {
	return &ExecProcessStarter{cfg: cfg}
}

func (s *ExecProcessStarter) StartGateway(ctx context.Context, spec GatewayStartSpec) (ManagedProcess, error) {
	if len(spec.Command) == 0 {
		return ManagedProcess{}, fmt.Errorf("gateway command is empty")
	}
	if err := os.MkdirAll(filepath.Join(spec.WorkspacePath, "home"), 0o750); err != nil {
		return ManagedProcess{}, fmt.Errorf("create gateway home: %w", err)
	}

	cmd := exec.CommandContext(context.Background(), spec.Command[0], spec.Command[1:]...)
	cmd.Env = spec.Env
	cmd.Dir = spec.WorkspacePath
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	configureGatewayCommand(cmd, spec.UID, spec.GID)

	if err := cmd.Start(); err != nil {
		return ManagedProcess{}, err
	}
	done := make(chan error, 1)
	notifyDone := make(chan error, 1)
	go func() {
		err := cmd.Wait()
		done <- err
		notifyDone <- err
	}()

	return ManagedProcess{
		PID:  cmd.Process.Pid,
		Done: notifyDone,
		Stop: func(stopCtx context.Context) error {
			timeout := s.cfg.ProcessStopTimeout
			if timeout <= 0 {
				timeout = 20 * time.Second
			}
			return stopGatewayCommand(stopCtx, cmd, done, timeout)
		},
	}, nil
}
