package hermes_test

import (
	"strings"
	"testing"

	"github.com/iamlovingit/clawmanager-agent/internal/gateway"
	"github.com/iamlovingit/clawmanager-agent/internal/runtime/hermes"
)

func TestProfileDefaults(t *testing.T) {
	profile := hermes.NewProfile("hermes")
	if profile.Type() != "hermes" {
		t.Fatalf("Type() = %q, want hermes", profile.Type())
	}
	if profile.DisplayName() != "Hermes" {
		t.Fatalf("DisplayName() = %q, want Hermes", profile.DisplayName())
	}
	defaults := profile.Defaults()
	if defaults.WorkspaceRoot != "/workspaces" {
		t.Fatalf("WorkspaceRoot = %q, want /workspaces", defaults.WorkspaceRoot)
	}
	if defaults.GatewayPortStart != 20000 || defaults.GatewayPortEnd != 20099 {
		t.Fatalf("port range = %d-%d, want 20000-20099", defaults.GatewayPortStart, defaults.GatewayPortEnd)
	}
	if defaults.GatewayPortBlockSize != 1 {
		t.Fatalf("GatewayPortBlockSize = %d, want 1", defaults.GatewayPortBlockSize)
	}
	if defaults.GatewayCapacity != 100 {
		t.Fatalf("GatewayCapacity = %d, want 100", defaults.GatewayCapacity)
	}
	if defaults.GatewayAuthMode != "trusted-proxy" {
		t.Fatalf("GatewayAuthMode = %q, want trusted-proxy", defaults.GatewayAuthMode)
	}
}

func TestGatewayCommandStartsHermesGateway(t *testing.T) {
	profile := hermes.NewProfile("hermes")
	command := strings.Join(profile.GatewayCommand("trusted-proxy"), " ")
	if command != "start-hermes-dashboard-gateway" {
		t.Fatalf("GatewayCommand() = %q, want start-hermes-dashboard-gateway", command)
	}
}

func TestGatewayEnvSetsHermesWorkspace(t *testing.T) {
	profile := hermes.NewProfile("hermes")
	req := gateway.CreateGatewayRequest{
		InstanceID: 63,
		UserID:     45,
		Environment: map[string]string{
			"CLAWMANAGER_LLM_API_KEY":      "secret",
			"CLAWMANAGER_TEAM_ENABLED":     "true",
			"CLAWMANAGER_TEAM_CONFIG_JSON": `{"teamId":"team-1","memberId":"leader"}`,
			"CLAWMANAGER_TEAM_SHARED_DIR":  "/team",
			"CUSTOM_RUNTIME_ENV":           "forwarded",
		},
	}
	workspacePath := "/workspaces/hermes/user-45/instance-63"
	env := profile.GatewayEnv(nil, gateway.Config{RuntimeType: "hermes", GatewayAuthMode: "trusted-proxy"}, req, workspacePath, 20017)
	values := envMap(env)
	if values["HOME"] != workspacePath+"/home" {
		t.Fatalf("HOME = %q", values["HOME"])
	}
	if values["HERMES_HOME"] != workspacePath+"/home/.hermes" {
		t.Fatalf("HERMES_HOME = %q", values["HERMES_HOME"])
	}
	if values["HOST"] != "0.0.0.0" || values["PORT"] != "20017" {
		t.Fatalf("host/port env = %#v", values)
	}
	if values["HERMES_ACCEPT_HOOKS"] != "1" {
		t.Fatalf("HERMES_ACCEPT_HOOKS = %q, want 1", values["HERMES_ACCEPT_HOOKS"])
	}
	if values["CLAWMANAGER_LLM_API_KEY"] != "secret" {
		t.Fatalf("CLAWMANAGER_LLM_API_KEY = %q, want secret", values["CLAWMANAGER_LLM_API_KEY"])
	}
	if values["CLAWMANAGER_TEAM_ENABLED"] != "true" {
		t.Fatalf("CLAWMANAGER_TEAM_ENABLED = %q, want true", values["CLAWMANAGER_TEAM_ENABLED"])
	}
	if values["CLAWMANAGER_TEAM_CONFIG_JSON"] != `{"teamId":"team-1","memberId":"leader"}` {
		t.Fatalf("CLAWMANAGER_TEAM_CONFIG_JSON = %q, want request value", values["CLAWMANAGER_TEAM_CONFIG_JSON"])
	}
	if values["CUSTOM_RUNTIME_ENV"] != "forwarded" {
		t.Fatalf("CUSTOM_RUNTIME_ENV = %q, want forwarded", values["CUSTOM_RUNTIME_ENV"])
	}
	if values["CLAWMANAGER_TEAM_CONFIG_PATH"] != workspacePath+"/team/team.json" {
		t.Fatalf("CLAWMANAGER_TEAM_CONFIG_PATH = %q, want workspace team config", values["CLAWMANAGER_TEAM_CONFIG_PATH"])
	}
	if values["CLAWMANAGER_TEAM_SHARED_DIR"] != workspacePath+"/team" {
		t.Fatalf("CLAWMANAGER_TEAM_SHARED_DIR = %q, want workspace team dir", values["CLAWMANAGER_TEAM_SHARED_DIR"])
	}
}

func TestGatewayEnvMovesDefaultTeamSharedDirIntoWorkspace(t *testing.T) {
	profile := hermes.NewProfile("hermes")
	workspacePath := "/workspaces/hermes/user-45/instance-64"
	req := gateway.CreateGatewayRequest{
		InstanceID: 64,
		UserID:     45,
		Environment: map[string]string{
			"CLAWMANAGER_TEAM_ENABLED":    "true",
			"CLAWMANAGER_TEAM_SHARED_DIR": "/team",
		},
	}

	env := profile.GatewayEnv(nil, gateway.Config{RuntimeType: "hermes", GatewayAuthMode: "trusted-proxy"}, req, workspacePath, 20018)
	values := envMap(env)
	if values["CLAWMANAGER_TEAM_SHARED_DIR"] != workspacePath+"/team" {
		t.Fatalf("CLAWMANAGER_TEAM_SHARED_DIR = %q, want workspace team dir", values["CLAWMANAGER_TEAM_SHARED_DIR"])
	}
	if _, ok := values["CLAWMANAGER_TEAM_CONFIG_PATH"]; ok {
		t.Fatalf("CLAWMANAGER_TEAM_CONFIG_PATH = %q, want unset without config JSON", values["CLAWMANAGER_TEAM_CONFIG_PATH"])
	}
}

func envMap(env []string) map[string]string {
	values := map[string]string{}
	for _, item := range env {
		key, value, ok := strings.Cut(item, "=")
		if ok {
			values[key] = value
		}
	}
	return values
}
