package hermes

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/iamlovingit/clawmanager-agent/internal/gateway"
)

func TestWriteGatewayConfigWritesHermesWorkspaceConfig(t *testing.T) {
	workspace := filepath.Join(t.TempDir(), "hermes", "user-45", "instance-63")
	hermesHome := filepath.Join(workspace, "home", ".hermes")
	if err := os.MkdirAll(hermesHome, 0o755); err != nil {
		t.Fatal(err)
	}
	configPath := filepath.Join(hermesHome, "config.yaml")
	if err := os.WriteFile(configPath, []byte("user_setting: keep\n"), 0o600); err != nil {
		t.Fatal(err)
	}

	req := gateway.CreateGatewayRequest{
		InstanceID: 63,
		UserID:     45,
		UID:        os.Getuid(),
		GID:        os.Getgid(),
		Environment: map[string]string{
			"CLAWMANAGER_LLM_BASE_URL":    "http://clawmanager-gateway.clawmanager-system.svc.cluster.local:9001/api/v1/gateway/llm",
			"CLAWMANAGER_LLM_API_KEY":     "instance-token",
			"CLAWMANAGER_LLM_MODEL":       `["auto","gpt-4.1"]`,
			"CLAWMANAGER_LLM_PROVIDER":    "openai-compatible",
			"CLAWMANAGER_INSTANCE_TOKEN":  "instance-token",
			"CLAWMANAGER_TEAM_SHARED_DIR": "/team",
			"CLAWMANAGER_TEAM_CONFIG_JSON": `{
				"teamId": "team-1",
				"members": [{"memberId": "leader"}]
			}`,
		},
	}
	cfg := gateway.Config{
		RuntimeType:     "hermes",
		GatewayAuthMode: "trusted-proxy",
		PublicOrigin:    "http://clawmanager-gateway.clawmanager-system.svc.cluster.local:9001",
		AllowedOrigins:  []string{"http://clawmanager-gateway.clawmanager-system.svc.cluster.local:9001"},
		TrustedProxies:  []string{"10.42.0.0/16"},
	}

	if err := WriteGatewayConfig(cfg, req, workspace); err != nil {
		t.Fatalf("WriteGatewayConfig() error = %v", err)
	}

	configData, err := os.ReadFile(configPath)
	if err != nil {
		t.Fatal(err)
	}
	configText := string(configData)
	for _, want := range []string{
		"user_setting: keep",
		"model:",
		"default: auto",
		"provider: clawmanager",
		"providers:",
		"base_url: http://clawmanager-gateway.clawmanager-system.svc.cluster.local:9001/api/v1/gateway/llm",
	} {
		if !strings.Contains(configText, want) {
			t.Fatalf("config.yaml missing %q:\n%s", want, configText)
		}
	}

	envData, err := os.ReadFile(filepath.Join(hermesHome, ".env"))
	if err != nil {
		t.Fatal(err)
	}
	envText := string(envData)
	for _, want := range []string{
		"OPENAI_API_KEY=instance-token",
		"OPENAI_BASE_URL=http://clawmanager-gateway.clawmanager-system.svc.cluster.local:9001/api/v1/gateway/llm",
	} {
		if !strings.Contains(envText, want) {
			t.Fatalf(".env missing %q:\n%s", want, envText)
		}
	}

	gatewayData, err := os.ReadFile(filepath.Join(hermesHome, "gateway.json"))
	if err != nil {
		t.Fatal(err)
	}
	var gatewayConfig map[string]any
	if err := json.Unmarshal(gatewayData, &gatewayConfig); err != nil {
		t.Fatal(err)
	}
	if gatewayConfig["base_path"] != "/api/v1/instances/63/proxy" {
		t.Fatalf("base_path = %#v", gatewayConfig["base_path"])
	}
	if gatewayConfig["auth_mode"] != "trusted-proxy" {
		t.Fatalf("auth_mode = %#v", gatewayConfig["auth_mode"])
	}
	if got := stringSlice(gatewayConfig["allowed_origins"]); len(got) != 1 || got[0] != cfg.PublicOrigin {
		t.Fatalf("allowed_origins = %#v", gatewayConfig["allowed_origins"])
	}
	if got := stringSlice(gatewayConfig["trusted_proxies"]); len(got) != 1 || got[0] != "10.42.0.0/16" {
		t.Fatalf("trusted_proxies = %#v", gatewayConfig["trusted_proxies"])
	}

	teamData, err := os.ReadFile(filepath.Join(workspace, "team", "team.json"))
	if err != nil {
		t.Fatalf("read lite team config: %v", err)
	}
	var teamConfig map[string]any
	if err := json.Unmarshal(teamData, &teamConfig); err != nil {
		t.Fatalf("parse lite team config: %v", err)
	}
	if teamConfig["teamId"] != "team-1" {
		t.Fatalf("teamId = %#v, want team-1", teamConfig["teamId"])
	}
}

func stringSlice(value any) []string {
	items, _ := value.([]any)
	out := make([]string, 0, len(items))
	for _, item := range items {
		if text, ok := item.(string); ok {
			out = append(out, text)
		}
	}
	return out
}
