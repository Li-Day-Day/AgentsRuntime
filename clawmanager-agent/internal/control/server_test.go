package control

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/iamlovingit/clawmanager-agent/internal/runtime/openclaw"
)

func TestControlHandlerRequiresControlToken(t *testing.T) {
	cfg := testConfig(t)
	mgr := NewGatewayManager(cfg, &fakeStarter{nextPID: 4242}, NewPortAllocator(func(int) bool { return false }))
	srv := httptest.NewServer(NewControlHandler(cfg, mgr, &fakeReporter{}))
	defer srv.Close()

	resp, err := http.Get(srv.URL + "/v1/health")
	if err != nil {
		t.Fatalf("GET /v1/health error = %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("GET /v1/health status = %d, want 401", resp.StatusCode)
	}
}

func TestControlHandlerCreatesIdempotentGatewayAndRejectsNoFreePort(t *testing.T) {
	cfg := testConfig(t)
	starter := &fakeStarter{nextPID: 4242}
	mgr := NewGatewayManager(cfg, starter, NewPortAllocator(func(int) bool { return false }))
	health := newBlockingHealthChecker()
	mgr.SetHealthChecker(health)
	srv := httptest.NewServer(NewControlHandler(cfg, mgr, &fakeReporter{}))
	defer srv.Close()

	req := testGatewayRequest(cfg.WorkspaceRoot, 123, 45, 7)
	resp := postGateway(t, srv.URL, cfg.ControlToken, req)
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("first create status = %d, want 200", resp.StatusCode)
	}
	var body CreateGatewayResponse
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		t.Fatalf("decode first create response: %v", err)
	}
	if body.GatewayID != "gw-123-7" || body.InstanceID != 123 || body.Port != 31000 || body.Status != "starting" || body.WorkspacePath != req.WorkspacePath {
		t.Fatalf("first create response = %+v, want starting gw-123-7 metadata on 31000", body)
	}
	eventually(t, func() bool { return starter.startCount() == 1 })

	again := postGateway(t, srv.URL, cfg.ControlToken, req)
	defer again.Body.Close()
	if again.StatusCode != http.StatusOK {
		t.Fatalf("idempotent create status = %d, want 200", again.StatusCode)
	}
	if starter.startCount() != 1 {
		t.Fatalf("gateway starts = %d, want one start for idempotent request", starter.startCount())
	}

	second := testGatewayRequest(cfg.WorkspaceRoot, 124, 45, 7)
	exhausted := postGateway(t, srv.URL, cfg.ControlToken, second)
	defer exhausted.Body.Close()
	if exhausted.StatusCode != http.StatusConflict {
		t.Fatalf("exhausted create status = %d, want 409", exhausted.StatusCode)
	}

	if states := mgr.GatewayStates(); len(states) != 1 || states[0].State != "starting" {
		t.Fatalf("GatewayStates before health = %+v, want one starting gateway", states)
	}
	health.succeed()
	eventually(t, func() bool {
		states := mgr.GatewayStates()
		return len(states) == 1 && states[0].State == "running" && states[0].PID == 4242
	})
}

func TestDrainRejectsNewGatewaysAndReportsHeartbeat(t *testing.T) {
	cfg := testConfig(t)
	reporter := &fakeReporter{}
	mgr := NewGatewayManager(cfg, &fakeStarter{nextPID: 4242}, NewPortAllocator(func(int) bool { return false }))
	srv := httptest.NewServer(NewControlHandler(cfg, mgr, reporter))
	defer srv.Close()

	drainBody := bytes.NewBufferString(`{"draining":true}`)
	req, err := http.NewRequest(http.MethodPost, srv.URL+"/v1/drain", drainBody)
	if err != nil {
		t.Fatalf("new drain request: %v", err)
	}
	req.Header.Set(ControlTokenHeader, cfg.ControlToken)
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("POST /v1/drain error = %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("drain status = %d, want 200", resp.StatusCode)
	}
	if !reporter.lastHeartbeat.Draining || reporter.lastHeartbeat.State != "draining" {
		t.Fatalf("drain heartbeat = %+v, want draining heartbeat", reporter.lastHeartbeat)
	}

	create := postGateway(t, srv.URL, cfg.ControlToken, testGatewayRequest(cfg.WorkspaceRoot, 123, 45, 7))
	defer create.Body.Close()
	if create.StatusCode != http.StatusConflict {
		t.Fatalf("create while draining status = %d, want 409", create.StatusCode)
	}
}

func TestManagerReportsCapacityFromAvailablePortBlocks(t *testing.T) {
	cfg := testConfig(t)
	cfg.GatewayPortStart = 31000
	cfg.GatewayPortEnd = 31008
	cfg.GatewayPortBlockSize = 3
	cfg.Capacity = 100
	mgr := NewGatewayManager(cfg, &fakeStarter{nextPID: 4242}, NewPortAllocator(func(int) bool { return false }))

	register := mgr.RegisterPayload()
	if register.Capacity != 3 || register.MaxGateways != 3 || register.AvailableSlots != 3 {
		t.Fatalf("RegisterPayload capacity = %+v, want max/available 3 from port blocks", register)
	}

	heartbeat := mgr.HeartbeatPayload(11)
	if heartbeat.MaxGateways != 3 || heartbeat.AvailableSlots != 3 {
		t.Fatalf("HeartbeatPayload capacity = %+v, want max/available 3 from port blocks", heartbeat)
	}
}

func testConfig(t *testing.T) Config {
	t.Helper()
	return Config{
		RuntimeType:          "openclaw",
		Runtime:              openclaw.NewProfile("openclaw"),
		WorkspaceRoot:        t.TempDir(),
		GatewayPortStart:     31000,
		GatewayPortEnd:       31000,
		GatewayPortBlockSize: 1,
		GatewayAuthMode:      "trusted-proxy",
		ControlToken:         "control-token",
		ReportToken:          "report-token",
		PublicOrigin:         "http://clawmanager-gateway.clawmanager-system.svc.cluster.local:9001",
		AllowedOrigins:       []string{"http://clawmanager-gateway.clawmanager-system.svc.cluster.local:9001"},
		GatewayToken:         "gateway-token",
		GatewayCommand:       []string{"openclaw", "gateway", "run", "--allow-unconfigured", "--auth", "trusted-proxy", "--bind", "auto", "--force"},
		ProcessStopTimeout:   0,
		Capacity:             100,
		Namespace:            "clawmanager-system",
		PodName:              "openclaw-runtime-test",
	}
}

func testGatewayRequest(root string, instanceID, userID, generation int) CreateGatewayRequest {
	return CreateGatewayRequest{
		InstanceID:    instanceID,
		UserID:        userID,
		AgentType:     "openclaw",
		WorkspacePath: filepath.Join(root, "openclaw", "user-"+itoa(userID), "instance-"+itoa(instanceID)),
		PortRange:     PortRange{Start: 31000, End: 31000},
		UID:           200000 + instanceID,
		GID:           200000 + instanceID,
		CPUCores:      2,
		MemoryMB:      4096,
		DiskQuotaMB:   20480,
		Generation:    generation,
	}
}

func postGateway(t *testing.T, baseURL, token string, payload CreateGatewayRequest) *http.Response {
	t.Helper()
	data, err := json.Marshal(payload)
	if err != nil {
		t.Fatalf("marshal gateway request: %v", err)
	}
	req, err := http.NewRequest(http.MethodPost, baseURL+"/v1/gateways", bytes.NewReader(data))
	if err != nil {
		t.Fatalf("new gateway request: %v", err)
	}
	req.Header.Set(ControlTokenHeader, token)
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatalf("POST /v1/gateways error = %v", err)
	}
	return resp
}

func itoa(v int) string {
	return strconv.Itoa(v)
}

type fakeStarter struct {
	mu      sync.Mutex
	nextPID int
	started []GatewayStartSpec
	stopped []int
}

func (f *fakeStarter) StartGateway(_ context.Context, spec GatewayStartSpec) (ManagedProcess, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.started = append(f.started, spec)
	pid := f.nextPID
	if pid == 0 {
		pid = 1
	}
	return ManagedProcess{PID: pid, Stop: func(context.Context) error {
		f.mu.Lock()
		defer f.mu.Unlock()
		f.stopped = append(f.stopped, pid)
		return nil
	}}, nil
}

func (f *fakeStarter) startCount() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.started)
}

func (f *fakeStarter) stopCount() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.stopped)
}

func (f *fakeStarter) startedSpec(index int) GatewayStartSpec {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.started[index]
}

type fakeReporter struct {
	lastHeartbeat HeartbeatPayload
}

func (f *fakeReporter) ReportHeartbeat(_ context.Context, payload HeartbeatPayload) error {
	f.lastHeartbeat = payload
	return nil
}

func TestCreateGatewayWritesConfigPassesTokenAndDoesNotReportRunningWhenOriginProbeFails(t *testing.T) {
	t.Setenv("OPENCLAW_GATEWAY_TOKEN", "ambient-token")
	t.Setenv("CLAWMANAGER_GATEWAY_TOKEN", "ambient-alias-token")
	cfg := testConfig(t)
	health := &fakeHealthChecker{err: ErrGatewayStartFailed}
	starter := &fakeStarter{nextPID: 4242}
	mgr := NewGatewayManager(cfg, starter, NewPortAllocator(func(int) bool { return false }))
	mgr.SetHealthChecker(health)

	resp, err := mgr.CreateGateway(context.Background(), testGatewayRequest(cfg.WorkspaceRoot, 63, 45, 7))
	if err != nil {
		t.Fatalf("CreateGateway() error = %v, want async starting response", err)
	}
	if resp.Status != "starting" {
		t.Fatalf("CreateGateway().Status = %q, want starting", resp.Status)
	}
	eventually(t, func() bool { return starter.startCount() == 1 })
	spec := starter.startedSpec(0)
	if !stringSlicesEqual(spec.Command, cfg.GatewayCommand) {
		t.Fatalf("gateway command = %#v, want %#v", spec.Command, cfg.GatewayCommand)
	}
	if got := envValue(spec.Env, "OPENCLAW_GATEWAY_TOKEN"); got != "" {
		t.Fatalf("OPENCLAW_GATEWAY_TOKEN = %q, want removed for trusted-proxy auth", got)
	}
	if got := envValue(spec.Env, "CLAWMANAGER_GATEWAY_TOKEN"); got != "" {
		t.Fatalf("CLAWMANAGER_GATEWAY_TOKEN = %q, want removed for trusted-proxy auth", got)
	}
	if got := envValue(spec.Env, "HOME"); got != filepath.Join(spec.WorkspacePath, "home") {
		t.Fatalf("HOME = %q, want workspace home", got)
	}
	configPath := filepath.Join(spec.WorkspacePath, "home", ".openclaw", "openclaw.json")
	data, readErr := os.ReadFile(configPath)
	if readErr != nil {
		t.Fatalf("OpenClaw config was not written before start: %v", readErr)
	}
	if !bytes.Contains(data, []byte(`"basePath": "/api/v1/instances/63/proxy"`)) {
		t.Fatalf("OpenClaw config missing instance proxy basePath: %s", string(data))
	}

	eventually(t, func() bool {
		states := mgr.GatewayStates()
		return len(states) == 1 && states[0].State == "error" && states[0].ErrorMessage != ""
	})
	eventually(t, func() bool { return starter.stopCount() == 1 })
	if mgr.UsedSlots() != 0 {
		t.Fatalf("UsedSlots = %d, want failed gateway excluded from capacity", mgr.UsedSlots())
	}
}

func TestControlHandlerPassesRequestEnvironmentToGatewayProcess(t *testing.T) {
	cfg := testConfig(t)
	redisTeamPlugin := filepath.Join(t.TempDir(), "redis-team")
	if err := os.MkdirAll(filepath.Join(redisTeamPlugin, "dist"), 0o755); err != nil {
		t.Fatalf("mkdir redis-team plugin: %v", err)
	}
	if err := os.WriteFile(filepath.Join(redisTeamPlugin, "openclaw.plugin.json"), []byte(`{"id":"redis-team","channels":["redis-team"]}`), 0o644); err != nil {
		t.Fatalf("write redis-team manifest: %v", err)
	}
	if err := os.WriteFile(filepath.Join(redisTeamPlugin, "dist", "index.js"), []byte(`module.exports = {};`), 0o644); err != nil {
		t.Fatalf("write redis-team entrypoint: %v", err)
	}
	t.Setenv("CLAWMANAGER_OPENCLAW_REDIS_TEAM_PLUGIN_DIR", redisTeamPlugin)

	starter := &fakeStarter{nextPID: 4242}
	mgr := NewGatewayManager(cfg, starter, NewPortAllocator(func(int) bool { return false }))
	mgr.SetHealthChecker(newBlockingHealthChecker())
	srv := httptest.NewServer(NewControlHandler(cfg, mgr, &fakeReporter{}))
	defer srv.Close()

	payload := testGatewayRequest(cfg.WorkspaceRoot, 123, 45, 7)
	payload.Environment = map[string]string{
		"CLAWMANAGER_TEAM_ENABLED":        "true",
		"CLAWMANAGER_TEAM_AUTORUN":        "true",
		"CLAWMANAGER_TEAM_CONFIG_JSON":    `{"teamId":"team-1","memberId":"leader"}`,
		"CLAWMANAGER_TEAM_CONSUMER_GROUP": "team-members",
		"CLAWMANAGER_TEAM_SHARED_DIR":     "/team",
		"CUSTOM_RUNTIME_ENV":              "forwarded",
	}

	resp := postGateway(t, srv.URL, cfg.ControlToken, payload)
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("create status = %d, want 200", resp.StatusCode)
	}
	eventually(t, func() bool { return starter.startCount() == 1 })

	spec := starter.startedSpec(0)
	for key, want := range payload.Environment {
		if key == "CLAWMANAGER_TEAM_SHARED_DIR" {
			want = filepath.Join(spec.WorkspacePath, "team")
		}
		if got := envValue(spec.Env, key); got != want {
			t.Fatalf("%s = %q, want %q", key, got, want)
		}
	}
	if got := envValue(spec.Env, "CLAWMANAGER_TEAM_CONFIG_PATH"); got != filepath.Join(spec.WorkspacePath, "team", "team.json") {
		t.Fatalf("CLAWMANAGER_TEAM_CONFIG_PATH = %q, want workspace team config", got)
	}
	if got := envValue(spec.Env, "CLAWMANAGER_TEAM_SHARED_DIR"); got != filepath.Join(spec.WorkspacePath, "team") {
		t.Fatalf("CLAWMANAGER_TEAM_SHARED_DIR = %q, want workspace team dir", got)
	}
}

func envValue(env []string, key string) string {
	prefix := key + "="
	for _, item := range env {
		if strings.HasPrefix(item, prefix) {
			return strings.TrimPrefix(item, prefix)
		}
	}
	return ""
}

func stringSlicesEqual(left, right []string) bool {
	if len(left) != len(right) {
		return false
	}
	for i := range left {
		if left[i] != right[i] {
			return false
		}
	}
	return true
}

type fakeHealthChecker struct {
	err error
}

func (f *fakeHealthChecker) WaitReady(context.Context, GatewayStartSpec) error {
	return f.err
}

type blockingHealthChecker struct {
	done chan error
}

func newBlockingHealthChecker() *blockingHealthChecker {
	return &blockingHealthChecker{done: make(chan error, 1)}
}

func (h *blockingHealthChecker) WaitReady(ctx context.Context, _ GatewayStartSpec) error {
	select {
	case err := <-h.done:
		return err
	case <-ctx.Done():
		return ctx.Err()
	}
}

func (h *blockingHealthChecker) succeed() {
	h.done <- nil
}

func eventually(t *testing.T, condition func() bool) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if condition() {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	if !condition() {
		t.Fatal("condition was not met before timeout")
	}
}
