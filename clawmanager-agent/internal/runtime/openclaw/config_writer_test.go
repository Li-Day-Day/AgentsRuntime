package openclaw

import (
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"testing"

	"github.com/iamlovingit/clawmanager-agent/internal/gateway"
)

type Config = gateway.Config
type CreateGatewayRequest = gateway.CreateGatewayRequest

func TestWriteOpenClawGatewayConfigMergesControlUIWithoutOverwritingExistingConfig(t *testing.T) {
	workspace := filepath.Join(t.TempDir(), "openclaw", "user-45", "instance-63")
	configPath := filepath.Join(workspace, "home", ".openclaw", "openclaw.json")
	if err := os.MkdirAll(filepath.Dir(configPath), 0o755); err != nil {
		t.Fatalf("mkdir config dir: %v", err)
	}
	existing := []byte(`{
	  "gateway": {
	    "auth": {"mode": "none", "legacy": true, "token": "stale-token"},
	    "controlUi": {
	      "theme": "dark",
	      "allowedOrigins": [
	        "http://localhost:20001",
	        "http://clawmanager-gateway.clawmanager-system.svc.cluster.local:9001"
	      ]
	    },
	    "trustedProxies": ["127.0.0.1"],
	    "keep": "value"
	  },
	  "agents": {"defaults": {"model": "auto/gpt-4.1"}}
	}`)
	if err := os.WriteFile(configPath, existing, 0o644); err != nil {
		t.Fatalf("write existing config: %v", err)
	}

	req := CreateGatewayRequest{InstanceID: 63, UserID: 45, UID: 200063, GID: 200063}
	cfg := Config{
		GatewayAuthMode: "trusted-proxy",
		PublicOrigin:    "http://clawmanager-gateway.clawmanager-system.svc.cluster.local:9001",
		AllowedOrigins:  []string{"http://clawmanager-gateway.clawmanager-system.svc.cluster.local:9001"},
		TrustedProxies:  []string{"127.0.0.1", "10.42.0.0/16"},
		LLMBaseURL:      "http://clawmanager-gateway.clawmanager-system.svc.cluster.local:9001/api/v1/gateway/llm",
		LLMAPIKey:       "runtime-llm-token",
		LLMAPIKeySet:    true,
		LLMModelIDs:     []string{"gpt-5.5", "gpt-5.5-mini"},
	}
	if err := WriteGatewayConfig(cfg, req, workspace); err != nil {
		t.Fatalf("WriteGatewayConfig() error = %v", err)
	}

	data, err := os.ReadFile(configPath)
	if err != nil {
		t.Fatalf("read merged config: %v", err)
	}
	var merged map[string]any
	if err := json.Unmarshal(data, &merged); err != nil {
		t.Fatalf("parse merged config: %v", err)
	}

	gateway := objectAt(t, merged, "gateway")
	auth := objectAt(t, gateway, "auth")
	if auth["mode"] != "trusted-proxy" {
		t.Fatalf("gateway.auth.mode = %#v, want trusted-proxy", auth["mode"])
	}
	if _, ok := auth["token"]; ok {
		t.Fatalf("gateway.auth.token was preserved in trusted-proxy mode")
	}
	trustedProxy := objectAt(t, auth, "trustedProxy")
	if trustedProxy["userHeader"] != "x-forwarded-prefix" {
		t.Fatalf("gateway.auth.trustedProxy.userHeader = %#v, want x-forwarded-prefix", trustedProxy["userHeader"])
	}
	allowUsers, ok := trustedProxy["allowUsers"].([]any)
	if !ok {
		t.Fatalf("gateway.auth.trustedProxy.allowUsers = %#v, want array", trustedProxy["allowUsers"])
	}
	if got := stringSet(allowUsers); len(got) != 1 || !got["/api/v1/instances/63/proxy"] {
		t.Fatalf("gateway.auth.trustedProxy.allowUsers = %#v, want instance proxy base path", allowUsers)
	}
	requiredHeaders, ok := trustedProxy["requiredHeaders"].([]any)
	if !ok {
		t.Fatalf("gateway.auth.trustedProxy.requiredHeaders = %#v, want array", trustedProxy["requiredHeaders"])
	}
	if got := stringSet(requiredHeaders); len(got) != 1 || !got["x-forwarded-proto"] {
		t.Fatalf("gateway.auth.trustedProxy.requiredHeaders = %#v, want x-forwarded-proto", requiredHeaders)
	}
	if auth["legacy"] != true {
		t.Fatalf("gateway.auth.legacy was not preserved")
	}
	if gateway["keep"] != "value" {
		t.Fatalf("gateway.keep = %#v, want preserved value", gateway["keep"])
	}
	controlUI := objectAt(t, gateway, "controlUi")
	if controlUI["theme"] != "dark" {
		t.Fatalf("gateway.controlUi.theme = %#v, want preserved value", controlUI["theme"])
	}
	if controlUI["basePath"] != "/api/v1/instances/63/proxy" {
		t.Fatalf("gateway.controlUi.basePath = %#v, want instance proxy base path", controlUI["basePath"])
	}
	if controlUI["dangerouslyDisableDeviceAuth"] != true {
		t.Fatalf("gateway.controlUi.dangerouslyDisableDeviceAuth = %#v, want true for trusted-proxy mode", controlUI["dangerouslyDisableDeviceAuth"])
	}
	origins, ok := controlUI["allowedOrigins"].([]any)
	if !ok {
		t.Fatalf("gateway.controlUi.allowedOrigins = %#v, want array", controlUI["allowedOrigins"])
	}
	if got := stringSet(origins); len(got) != 2 || !got["http://localhost:20001"] || !got["http://clawmanager-gateway.clawmanager-system.svc.cluster.local:9001"] {
		t.Fatalf("allowedOrigins = %#v, want existing origin plus deduped ClawManager service origin", origins)
	}
	trustedProxies, ok := gateway["trustedProxies"].([]any)
	if !ok {
		t.Fatalf("gateway.trustedProxies = %#v, want array", gateway["trustedProxies"])
	}
	if got := stringSet(trustedProxies); len(got) != 2 || !got["127.0.0.1"] || !got["10.42.0.0/16"] {
		t.Fatalf("trustedProxies = %#v, want existing proxy plus deduped pod-network CIDR", trustedProxies)
	}
	agents := objectAt(t, merged, "agents")
	defaults := objectAt(t, agents, "defaults")
	model := objectAt(t, defaults, "model")
	if model["primary"] != "auto/gpt-5.5" {
		t.Fatalf("agents.defaults.model.primary = %#v, want injected primary model", model["primary"])
	}
	agentModels := objectAt(t, defaults, "models")
	if _, ok := agentModels["auto/gpt-5.5"]; !ok {
		t.Fatalf("agents.defaults.models missing auto/gpt-5.5: %#v", agentModels)
	}
	if _, ok := agentModels["auto/gpt-5.5-mini"]; !ok {
		t.Fatalf("agents.defaults.models missing auto/gpt-5.5-mini: %#v", agentModels)
	}
	models := objectAt(t, merged, "models")
	providers := objectAt(t, models, "providers")
	autoProvider := objectAt(t, providers, "auto")
	if autoProvider["baseUrl"] != "http://clawmanager-gateway.clawmanager-system.svc.cluster.local:9001/api/v1/gateway/llm" {
		t.Fatalf("models.providers.auto.baseUrl = %#v, want injected ClawManager LLM gateway", autoProvider["baseUrl"])
	}
	if autoProvider["apiKey"] != "runtime-llm-token" {
		t.Fatalf("models.providers.auto.apiKey = %#v, want injected runtime token", autoProvider["apiKey"])
	}
	if autoProvider["auth"] != "api-key" {
		t.Fatalf("models.providers.auto.auth = %#v, want api-key", autoProvider["auth"])
	}
	if autoProvider["api"] != "openai-completions" {
		t.Fatalf("models.providers.auto.api = %#v, want openai-completions", autoProvider["api"])
	}
	providerModels, ok := autoProvider["models"].([]any)
	if !ok {
		t.Fatalf("models.providers.auto.models = %#v, want array", autoProvider["models"])
	}
	if got := modelIDSet(providerModels); len(got) != 2 || !got["gpt-5.5"] || !got["gpt-5.5-mini"] {
		t.Fatalf("models.providers.auto.models = %#v, want injected model ids", providerModels)
	}
	if runtime.GOOS != "windows" {
		info, err := os.Stat(configPath)
		if err != nil {
			t.Fatalf("stat config: %v", err)
		}
		if info.Mode().Perm() != 0o600 {
			t.Fatalf("config mode = %o, want 0600", info.Mode().Perm())
		}
	}
}

func TestWriteOpenClawGatewayConfigUsesRequestEnvironmentLLMOverrides(t *testing.T) {
	workspace := filepath.Join(t.TempDir(), "openclaw", "user-45", "instance-68")
	req := CreateGatewayRequest{
		InstanceID: 68,
		UserID:     45,
		UID:        200068,
		GID:        200068,
		Environment: map[string]string{
			"CLAWMANAGER_LLM_BASE_URL": "http://clawmanager-gateway.clawmanager-system.svc.cluster.local:9001/api/v1/gateway/llm",
			"CLAWMANAGER_LLM_API_KEY":  "instance-token",
			"CLAWMANAGER_LLM_MODEL":    `["auto","gpt-5.5"]`,
		},
	}
	cfg := Config{GatewayAuthMode: "trusted-proxy"}

	if err := WriteGatewayConfig(cfg, req, workspace); err != nil {
		t.Fatalf("WriteGatewayConfig() error = %v", err)
	}

	data, err := os.ReadFile(filepath.Join(workspace, "home", ".openclaw", "openclaw.json"))
	if err != nil {
		t.Fatalf("read config: %v", err)
	}
	var merged map[string]any
	if err := json.Unmarshal(data, &merged); err != nil {
		t.Fatalf("parse config: %v", err)
	}
	autoProvider := objectAt(t, objectAt(t, objectAt(t, merged, "models"), "providers"), "auto")
	if autoProvider["apiKey"] != "instance-token" {
		t.Fatalf("models.providers.auto.apiKey = %#v, want request token", autoProvider["apiKey"])
	}
	defaults := objectAt(t, objectAt(t, merged, "agents"), "defaults")
	model := objectAt(t, defaults, "model")
	if model["primary"] != "auto/auto" {
		t.Fatalf("agents.defaults.model.primary = %#v, want first request model", model["primary"])
	}
}

func TestWriteOpenClawGatewayConfigWritesLiteTeamConfigJSON(t *testing.T) {
	workspace := filepath.Join(t.TempDir(), "openclaw", "user-45", "instance-69")
	req := CreateGatewayRequest{
		InstanceID: 69,
		UserID:     45,
		UID:        200069,
		GID:        200069,
		Environment: map[string]string{
			"CLAWMANAGER_TEAM_CONFIG_JSON": `{"teamId":"team-1","members":[{"memberId":"leader"}]}`,
			"CLAWMANAGER_TEAM_SHARED_DIR":  "/team",
		},
	}

	if err := WriteGatewayConfig(Config{GatewayAuthMode: "trusted-proxy"}, req, workspace); err != nil {
		t.Fatalf("WriteGatewayConfig() error = %v", err)
	}

	data, err := os.ReadFile(filepath.Join(workspace, "team", "team.json"))
	if err != nil {
		t.Fatalf("read lite team config: %v", err)
	}
	var teamConfig map[string]any
	if err := json.Unmarshal(data, &teamConfig); err != nil {
		t.Fatalf("parse lite team config: %v", err)
	}
	if teamConfig["teamId"] != "team-1" {
		t.Fatalf("teamId = %#v, want team-1", teamConfig["teamId"])
	}
}

func TestWriteOpenClawGatewayConfigEnablesRedisTeamForLiteTeam(t *testing.T) {
	root := t.TempDir()
	workspace := filepath.Join(root, "openclaw", "user-1", "instance-106")
	sourcePlugin := filepath.Join(root, "defaults", ".openclaw", "extensions", "redis-team")
	if err := os.MkdirAll(filepath.Join(sourcePlugin, "dist"), 0o755); err != nil {
		t.Fatalf("mkdir source plugin: %v", err)
	}
	if err := os.WriteFile(filepath.Join(sourcePlugin, "openclaw.plugin.json"), []byte(`{"id":"redis-team","channels":["redis-team"]}`), 0o644); err != nil {
		t.Fatalf("write source manifest: %v", err)
	}
	if err := os.WriteFile(filepath.Join(sourcePlugin, "dist", "index.js"), []byte(`module.exports = {};`), 0o644); err != nil {
		t.Fatalf("write source plugin entrypoint: %v", err)
	}
	t.Setenv("CLAWMANAGER_OPENCLAW_REDIS_TEAM_PLUGIN_DIR", sourcePlugin)

	req := CreateGatewayRequest{
		InstanceID: 106,
		UserID:     1,
		UID:        200106,
		GID:        200106,
		Environment: map[string]string{
			"CLAWMANAGER_TEAM_ENABLED":        "true",
			"CLAWMANAGER_TEAM_AUTORUN":        "true",
			"CLAWMANAGER_TEAM_REDIS_URL":      "redis://clawmanager-team-redis:6379/0",
			"CLAWMANAGER_TEAM_ID":             "26",
			"CLAWMANAGER_TEAM_MEMBER_ID":      "leader",
			"CLAWMANAGER_TEAM_ROLE":           "leader",
			"CLAWMANAGER_TEAM_INBOX_KEY":      "claw:team:26:inbox:leader",
			"CLAWMANAGER_TEAM_EVENTS_KEY":     "claw:team:26:events",
			"CLAWMANAGER_TEAM_PRESENCE_KEY":   "claw:team:26:presence",
			"CLAWMANAGER_TEAM_DLQ_KEY":        "claw:team:26:dlq:leader",
			"CLAWMANAGER_TEAM_CONSUMER_GROUP": "team-members",
			"CLAWMANAGER_TEAM_SHARED_DIR":     "/team",
		},
	}

	if err := WriteGatewayConfig(Config{GatewayAuthMode: "trusted-proxy"}, req, workspace); err != nil {
		t.Fatalf("WriteGatewayConfig() error = %v", err)
	}

	data, err := os.ReadFile(filepath.Join(workspace, "home", ".openclaw", "openclaw.json"))
	if err != nil {
		t.Fatalf("read config: %v", err)
	}
	var cfg map[string]any
	if err := json.Unmarshal(data, &cfg); err != nil {
		t.Fatalf("parse config: %v", err)
	}
	pluginEntry := objectAt(t, objectAt(t, objectAt(t, cfg, "plugins"), "entries"), "redis-team")
	if got := pluginEntry["enabled"]; got != true {
		t.Fatalf("plugins.entries.redis-team.enabled = %#v, want true", got)
	}
	account := objectAt(t, objectAt(t, objectAt(t, objectAt(t, cfg, "channels"), "redis-team"), "accounts"), "default")
	if got := account["fromEnv"]; got != true {
		t.Fatalf("channels.redis-team.accounts.default.fromEnv = %#v, want true", got)
	}
	if got := account["enabled"]; got != true {
		t.Fatalf("channels.redis-team.accounts.default.enabled = %#v, want true", got)
	}
	if got := account["autoRun"]; got != true {
		t.Fatalf("channels.redis-team.accounts.default.autoRun = %#v, want true", got)
	}
	if got := account["redisUrl"]; got != "redis://clawmanager-team-redis:6379/0" {
		t.Fatalf("channels.redis-team.accounts.default.redisUrl = %#v, want request redis url", got)
	}
	if got := account["memberId"]; got != "leader" {
		t.Fatalf("channels.redis-team.accounts.default.memberId = %#v, want leader", got)
	}
	if got := account["inboxKey"]; got != "claw:team:26:inbox:leader" {
		t.Fatalf("channels.redis-team.accounts.default.inboxKey = %#v, want leader inbox key", got)
	}
	if got := account["eventsKey"]; got != "claw:team:26:events" {
		t.Fatalf("channels.redis-team.accounts.default.eventsKey = %#v, want team events key", got)
	}
	if got := account["presenceKey"]; got != "claw:team:26:presence" {
		t.Fatalf("channels.redis-team.accounts.default.presenceKey = %#v, want team presence key", got)
	}
	if got := account["consumerGroup"]; got != "team-members" {
		t.Fatalf("channels.redis-team.accounts.default.consumerGroup = %#v, want team-members", got)
	}
	if got := account["sharedDir"]; got != filepath.Join(workspace, "team") {
		t.Fatalf("channels.redis-team.accounts.default.sharedDir = %#v, want remapped workspace team dir", got)
	}

	copiedManifest := filepath.Join(workspace, "home", ".openclaw", "extensions", "redis-team", "openclaw.plugin.json")
	if _, err := os.Stat(copiedManifest); err != nil {
		t.Fatalf("expected redis-team plugin manifest to be seeded at %s: %v", copiedManifest, err)
	}
}

func objectAt(t *testing.T, parent map[string]any, key string) map[string]any {
	t.Helper()
	value, ok := parent[key].(map[string]any)
	if !ok {
		t.Fatalf("%s = %#v, want object", key, parent[key])
	}
	return value
}

func stringSet(values []any) map[string]bool {
	out := map[string]bool{}
	for _, value := range values {
		if text, ok := value.(string); ok {
			out[text] = true
		}
	}
	return out
}

func modelIDSet(values []any) map[string]bool {
	out := map[string]bool{}
	for _, value := range values {
		model, ok := value.(map[string]any)
		if !ok {
			continue
		}
		id, ok := model["id"].(string)
		if ok {
			out[id] = true
		}
	}
	return out
}
