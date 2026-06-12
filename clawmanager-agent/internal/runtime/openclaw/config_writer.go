package openclaw

import (
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/iamlovingit/clawmanager-agent/internal/gateway"
)

const openClawTrustedProxyUserHeader = "x-forwarded-prefix"
const openClawTrustedProxyRequiredHeader = "x-forwarded-proto"
const openClawAutoProviderName = "auto"
const openClawRedisTeamPluginID = "redis-team"
const openClawRedisTeamPluginDirEnv = "CLAWMANAGER_OPENCLAW_REDIS_TEAM_PLUGIN_DIR"

func WriteGatewayConfig(cfg gateway.Config, req gateway.CreateGatewayRequest, workspacePath string) error {
	if err := gateway.WriteLiteTeamConfigJSON(req, workspacePath); err != nil {
		return err
	}
	resolvedCfg, err := configWithRequestLLMEnv(cfg, req)
	if err != nil {
		return err
	}
	cfg = resolvedCfg

	configPath := filepath.Join(workspacePath, "home", ".openclaw", "openclaw.json")
	if err := os.MkdirAll(filepath.Dir(configPath), 0o750); err != nil {
		return fmt.Errorf("create openclaw config dir: %w", err)
	}

	config := map[string]any{}
	if data, err := os.ReadFile(configPath); err == nil && len(data) > 0 {
		if err := json.Unmarshal(data, &config); err != nil {
			return fmt.Errorf("parse openclaw config: %w", err)
		}
	} else if err != nil && !os.IsNotExist(err) {
		return fmt.Errorf("read openclaw config: %w", err)
	}

	if teamEnabledFromRequest(req) {
		if err := configureOpenClawRedisTeam(config, req, workspacePath); err != nil {
			return err
		}
		if err := seedOpenClawRedisTeamPlugin(req, workspacePath); err != nil {
			return err
		}
	}

	gatewayConfig := ensureObject(config, "gateway")
	auth := ensureObject(gatewayConfig, "auth")
	basePath := "/api/v1/instances/" + strconv.Itoa(req.InstanceID) + "/proxy"
	if cfg.GatewayAuthMode == "token" {
		auth["mode"] = "token"
	} else {
		auth["mode"] = "trusted-proxy"
		delete(auth, "token")
		trustedProxy := ensureObject(auth, "trustedProxy")
		trustedProxy["userHeader"] = openClawTrustedProxyUserHeader
		trustedProxy["requiredHeaders"] = []string{openClawTrustedProxyRequiredHeader}
		trustedProxy["allowUsers"] = []string{basePath}
	}

	controlUI := ensureObject(gatewayConfig, "controlUi")
	controlUI["basePath"] = basePath
	if cfg.GatewayAuthMode == "trusted-proxy" {
		controlUI["dangerouslyDisableDeviceAuth"] = true
	}
	origins := cfg.AllowedOrigins
	if len(origins) == 0 && cfg.PublicOrigin != "" {
		origins = []string{cfg.PublicOrigin}
	}
	if len(origins) > 0 {
		controlUI["allowedOrigins"] = appendUniqueStringArray(controlUI["allowedOrigins"], origins...)
	}
	if len(cfg.TrustedProxies) > 0 {
		gatewayConfig["trustedProxies"] = appendUniqueStringArray(gatewayConfig["trustedProxies"], cfg.TrustedProxies...)
	}
	mergeOpenClawLLMConfig(config, cfg)

	data, err := json.MarshalIndent(config, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal openclaw config: %w", err)
	}
	if err := os.WriteFile(configPath, append(data, '\n'), 0o600); err != nil {
		return fmt.Errorf("write openclaw config: %w", err)
	}
	if err := os.Chmod(configPath, 0o600); err != nil {
		return fmt.Errorf("chmod openclaw config: %w", err)
	}
	if err := gateway.ChownWorkspace(filepath.Dir(configPath), req.UID, req.GID); err != nil {
		return fmt.Errorf("chown openclaw config dir: %w", err)
	}
	if err := gateway.ChownWorkspace(configPath, req.UID, req.GID); err != nil {
		return fmt.Errorf("chown openclaw config: %w", err)
	}
	return nil
}

func configureOpenClawRedisTeam(config map[string]any, req gateway.CreateGatewayRequest, workspacePath string) error {
	plugins := ensureObject(config, "plugins")
	entries := ensureObject(plugins, "entries")
	entry := ensureObject(entries, openClawRedisTeamPluginID)
	entry["enabled"] = true

	channels := ensureObject(config, "channels")
	channel := ensureObject(channels, openClawRedisTeamPluginID)
	accounts := ensureObject(channel, "accounts")
	account := ensureObject(accounts, "default")
	account["fromEnv"] = true
	account["enabled"] = true

	setTeamStringAccountValue(account, req, "CLAWMANAGER_TEAM_REDIS_URL", "redisUrl")
	setTeamStringAccountValue(account, req, "CLAWMANAGER_TEAM_ID", "teamId")
	setTeamStringAccountValue(account, req, "CLAWMANAGER_TEAM_MEMBER_ID", "memberId")
	setTeamStringAccountValue(account, req, "CLAWMANAGER_TEAM_ROLE", "role")
	setTeamStringAccountValue(account, req, "CLAWMANAGER_TEAM_MANAGER_URL", "managerUrl")
	setTeamStringAccountValue(account, req, "CLAWMANAGER_TEAM_CONSUMER_GROUP", "consumerGroup")
	setTeamStringAccountValue(account, req, "CLAWMANAGER_TEAM_INBOX_KEY", "inboxKey")
	setTeamStringAccountValue(account, req, "CLAWMANAGER_TEAM_EVENTS_KEY", "eventsKey")
	setTeamStringAccountValue(account, req, "CLAWMANAGER_TEAM_PRESENCE_KEY", "presenceKey")
	setTeamStringAccountValue(account, req, "CLAWMANAGER_TEAM_DLQ_KEY", "dlqKey")
	setTeamBoolAccountValue(account, req, "CLAWMANAGER_TEAM_AUTORUN", "autoRun")
	setTeamIntAccountValue(account, req, "CLAWMANAGER_TEAM_EMBEDDED_TIMEOUT_SECONDS", "embeddedTimeoutSeconds")

	if _, sharedDir, ok := gateway.LiteTeamEnvironment(req, workspacePath); ok && strings.TrimSpace(sharedDir) != "" {
		account["sharedDir"] = sharedDir
		sharedDirPath := filepath.FromSlash(sharedDir)
		if err := os.MkdirAll(sharedDirPath, 0o750); err != nil {
			return fmt.Errorf("create lite team shared dir: %w", err)
		}
		if err := gateway.ChownWorkspace(sharedDirPath, req.UID, req.GID); err != nil {
			return fmt.Errorf("chown lite team shared dir: %w", err)
		}
	}
	return nil
}

func setTeamStringAccountValue(account map[string]any, req gateway.CreateGatewayRequest, envKey, configKey string) {
	value, ok := requestEnvValue(req, envKey)
	if !ok {
		return
	}
	value = strings.TrimSpace(value)
	if value == "" {
		return
	}
	account[configKey] = value
}

func setTeamBoolAccountValue(account map[string]any, req gateway.CreateGatewayRequest, envKey, configKey string) {
	value, ok := requestEnvValue(req, envKey)
	if !ok {
		return
	}
	account[configKey] = truthyOpenClawTeamEnv(value)
}

func setTeamIntAccountValue(account map[string]any, req gateway.CreateGatewayRequest, envKey, configKey string) {
	value, ok := requestEnvValue(req, envKey)
	if !ok {
		return
	}
	parsed, err := strconv.Atoi(strings.TrimSpace(value))
	if err != nil || parsed <= 0 {
		return
	}
	account[configKey] = parsed
}

func teamEnabledFromRequest(req gateway.CreateGatewayRequest) bool {
	value, ok := requestEnvValue(req, "CLAWMANAGER_TEAM_ENABLED")
	return ok && truthyOpenClawTeamEnv(value)
}

func truthyOpenClawTeamEnv(value string) bool {
	switch strings.ToLower(strings.TrimSpace(value)) {
	case "1", "true", "yes", "y", "on", "enabled":
		return true
	default:
		return false
	}
}

func seedOpenClawRedisTeamPlugin(req gateway.CreateGatewayRequest, workspacePath string) error {
	source, ok, err := findOpenClawRedisTeamPluginSource()
	if err != nil {
		return err
	}
	if !ok {
		return fmt.Errorf("redis-team plugin source not found; checked %s and default OpenClaw extension locations", openClawRedisTeamPluginDirEnv)
	}
	extensionsDir := filepath.Join(workspacePath, "home", ".openclaw", "extensions")
	target := filepath.Join(extensionsDir, openClawRedisTeamPluginID)
	if err := copyDir(source, target); err != nil {
		return fmt.Errorf("seed redis-team plugin: %w", err)
	}
	if err := chownTree(extensionsDir, req.UID, req.GID); err != nil {
		return fmt.Errorf("chown redis-team plugin: %w", err)
	}
	return nil
}

func findOpenClawRedisTeamPluginSource() (string, bool, error) {
	candidates := []string{}
	if value := strings.TrimSpace(os.Getenv(openClawRedisTeamPluginDirEnv)); value != "" {
		candidates = append(candidates, value)
	}
	candidates = append(candidates,
		"/config/.openclaw/extensions/redis-team",
		"/defaults/.openclaw/extensions/redis-team",
	)
	for _, candidate := range candidates {
		clean := filepath.Clean(candidate)
		info, err := os.Stat(clean)
		if err == nil {
			if !info.IsDir() {
				return "", false, fmt.Errorf("redis-team plugin source is not a directory: %s", clean)
			}
			manifest := filepath.Join(clean, "openclaw.plugin.json")
			if _, err := os.Stat(manifest); err != nil {
				return "", false, fmt.Errorf("redis-team plugin source missing manifest %s: %w", manifest, err)
			}
			return clean, true, nil
		}
		if os.IsNotExist(err) {
			continue
		}
		return "", false, fmt.Errorf("stat redis-team plugin source %s: %w", clean, err)
	}
	return "", false, nil
}

func copyDir(source, target string) error {
	info, err := os.Stat(source)
	if err != nil {
		return err
	}
	if !info.IsDir() {
		return fmt.Errorf("%s is not a directory", source)
	}
	if err := os.MkdirAll(target, info.Mode().Perm()); err != nil {
		return err
	}
	entries, err := os.ReadDir(source)
	if err != nil {
		return err
	}
	for _, entry := range entries {
		srcPath := filepath.Join(source, entry.Name())
		dstPath := filepath.Join(target, entry.Name())
		entryInfo, err := entry.Info()
		if err != nil {
			return err
		}
		if entryInfo.IsDir() {
			if err := copyDir(srcPath, dstPath); err != nil {
				return err
			}
			continue
		}
		if !entryInfo.Mode().IsRegular() {
			continue
		}
		if err := copyRegularFile(srcPath, dstPath, entryInfo.Mode().Perm()); err != nil {
			return err
		}
	}
	return nil
}

func copyRegularFile(source, target string, mode os.FileMode) error {
	src, err := os.Open(source)
	if err != nil {
		return err
	}
	defer src.Close()
	if err := os.MkdirAll(filepath.Dir(target), 0o750); err != nil {
		return err
	}
	dst, err := os.OpenFile(target, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, mode)
	if err != nil {
		return err
	}
	if _, err := io.Copy(dst, src); err != nil {
		_ = dst.Close()
		return err
	}
	if err := dst.Close(); err != nil {
		return err
	}
	return os.Chmod(target, mode)
}

func chownTree(root string, uid, gid int) error {
	return filepath.WalkDir(root, func(path string, _ os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		return gateway.ChownWorkspace(path, uid, gid)
	})
}

func configWithRequestLLMEnv(cfg gateway.Config, req gateway.CreateGatewayRequest) (gateway.Config, error) {
	resolved := cfg
	if value, ok := requestEnvValue(req, "CLAWMANAGER_LLM_BASE_URL", "OPENAI_BASE_URL", "OPENAI_API_BASE"); ok && strings.TrimSpace(value) != "" {
		resolved.LLMBaseURL = strings.TrimSpace(value)
	}
	if value, ok := requestEnvValue(req, "CLAWMANAGER_LLM_API_KEY", "OPENAI_API_KEY"); ok {
		resolved.LLMAPIKey = value
		resolved.LLMAPIKeySet = true
	}
	if raw, ok := requestEnvValue(req, "CLAWMANAGER_LLM_MODEL", "OPENAI_MODEL"); ok && strings.TrimSpace(raw) != "" {
		modelIDs, err := parseLLMModelIDs(raw)
		if err != nil {
			return gateway.Config{}, err
		}
		resolved.LLMModelIDs = modelIDs
	}
	return resolved, nil
}

func requestEnvValue(req gateway.CreateGatewayRequest, keys ...string) (string, bool) {
	for _, key := range keys {
		if req.Environment != nil {
			if value, ok := req.Environment[key]; ok {
				return value, true
			}
		}
		if req.Env != nil {
			if value, ok := req.Env[key]; ok {
				return value, true
			}
		}
	}
	return "", false
}

func mergeOpenClawLLMConfig(config map[string]any, cfg gateway.Config) {
	if cfg.LLMBaseURL == "" && !cfg.LLMAPIKeySet && len(cfg.LLMModelIDs) == 0 {
		normalizeOpenClawProviderAuthContracts(config)
		return
	}

	models := ensureObject(config, "models")
	providers := ensureObject(models, "providers")
	provider := ensureObject(providers, openClawAutoProviderName)
	if cfg.LLMBaseURL != "" {
		provider["baseUrl"] = cfg.LLMBaseURL
	}
	if cfg.LLMAPIKeySet {
		provider["apiKey"] = cfg.LLMAPIKey
	}
	if strings.TrimSpace(configStringValue(provider["api"])) == "" {
		provider["api"] = "openai-completions"
	}
	if strings.TrimSpace(configStringValue(provider["auth"])) == "" && strings.TrimSpace(cfg.LLMAPIKey) != "" {
		provider["auth"] = "api-key"
	}
	if len(cfg.LLMModelIDs) > 0 {
		provider["models"] = buildOpenClawProviderModels(provider["models"], cfg.LLMModelIDs)

		agents := ensureObject(config, "agents")
		defaults := ensureObject(agents, "defaults")
		model := ensureObject(defaults, "model")
		model["primary"] = qualifiedOpenClawModelID(openClawAutoProviderName, cfg.LLMModelIDs[0])
		defaults["models"] = buildOpenClawAgentModels(defaults["models"], openClawAutoProviderName, cfg.LLMModelIDs)
	}
	normalizeOpenClawProviderAuthContracts(config)
}

func normalizeOpenClawProviderAuthContracts(config map[string]any) {
	models, ok := config["models"].(map[string]any)
	if !ok {
		return
	}
	providers, ok := models["providers"].(map[string]any)
	if !ok {
		return
	}
	for _, raw := range providers {
		provider, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		if strings.TrimSpace(configStringValue(provider["auth"])) != "" {
			continue
		}
		if strings.TrimSpace(configStringValue(provider["apiKey"])) == "" {
			continue
		}
		provider["auth"] = "api-key"
	}
}

func buildOpenClawProviderModels(existing any, modelIDs []string) []any {
	byID := indexOpenClawModelsByID(existing)
	models := make([]any, 0, len(modelIDs))
	for _, id := range modelIDs {
		if current, ok := byID[id]; ok {
			cloned := cloneOpenClawMap(current)
			cloned["id"] = id
			if strings.EqualFold(id, "auto") || strings.TrimSpace(configStringValue(cloned["name"])) == "" {
				cloned["name"] = displayOpenClawModelName(id)
			}
			models = append(models, cloned)
			continue
		}
		models = append(models, defaultOpenClawProviderModel(id))
	}
	return models
}

func indexOpenClawModelsByID(existing any) map[string]map[string]any {
	items, ok := existing.([]any)
	if !ok {
		return map[string]map[string]any{}
	}
	index := make(map[string]map[string]any, len(items))
	for _, item := range items {
		model, ok := item.(map[string]any)
		if !ok {
			continue
		}
		id := strings.TrimSpace(configStringValue(model["id"]))
		if id != "" {
			index[id] = model
		}
	}
	return index
}

func buildOpenClawAgentModels(existing any, providerName string, modelIDs []string) map[string]any {
	current, _ := existing.(map[string]any)
	models := make(map[string]any, len(modelIDs))
	for _, id := range modelIDs {
		key := qualifiedOpenClawModelID(providerName, id)
		if current != nil {
			if value, ok := current[key]; ok {
				models[key] = value
				continue
			}
		}
		models[key] = map[string]any{}
	}
	return models
}

func defaultOpenClawProviderModel(id string) map[string]any {
	return map[string]any{
		"id":        id,
		"name":      displayOpenClawModelName(id),
		"reasoning": false,
		"input": []any{
			"text",
		},
		"cost": map[string]any{
			"input":      0,
			"output":     0,
			"cacheRead":  0,
			"cacheWrite": 0,
		},
		"contextWindow": 1000000,
		"maxTokens":     65536,
	}
}

func qualifiedOpenClawModelID(providerName, id string) string {
	return providerName + "/" + id
}

func displayOpenClawModelName(id string) string {
	if strings.EqualFold(id, "auto") {
		return "Auto"
	}
	return id
}

func cloneOpenClawMap(source map[string]any) map[string]any {
	cloned := make(map[string]any, len(source))
	for key, value := range source {
		cloned[key] = value
	}
	return cloned
}

func configStringValue(value any) string {
	switch raw := value.(type) {
	case string:
		return raw
	case nil:
		return ""
	default:
		return strings.TrimSpace(fmt.Sprint(value))
	}
}

func ensureObject(parent map[string]any, key string) map[string]any {
	if existing, ok := parent[key].(map[string]any); ok {
		return existing
	}
	next := map[string]any{}
	parent[key] = next
	return next
}

func appendUniqueStringArray(existing any, values ...string) []string {
	seen := map[string]bool{}
	out := []string{}
	switch typed := existing.(type) {
	case []any:
		for _, item := range typed {
			text, ok := item.(string)
			if ok && text != "" && !seen[text] {
				seen[text] = true
				out = append(out, text)
			}
		}
	case []string:
		for _, text := range typed {
			if text != "" && !seen[text] {
				seen[text] = true
				out = append(out, text)
			}
		}
	}
	for _, text := range values {
		if text != "" && !seen[text] {
			seen[text] = true
			out = append(out, text)
		}
	}
	return out
}
