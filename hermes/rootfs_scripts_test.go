package hermesimage_test

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestDashboardGatewayScriptStartsRedisTeamConsumerWhenAutorunEnabled(t *testing.T) {
	data, err := os.ReadFile(filepath.Join("rootfs", "usr", "local", "bin", "start-hermes-dashboard-gateway"))
	if err != nil {
		t.Fatalf("read start-hermes-dashboard-gateway: %v", err)
	}
	script := string(data)
	for _, want := range []string{
		"CLAWMANAGER_TEAM_ENABLED",
		"CLAWMANAGER_TEAM_AUTORUN",
		"CLAWMANAGER_TEAM_REDIS_URL",
		"CLAWMANAGER_TEAM_ID",
		"CLAWMANAGER_TEAM_MEMBER_ID",
		"hermes gateway run --accept-hooks --no-supervise",
	} {
		if !strings.Contains(script, want) {
			t.Fatalf("start-hermes-dashboard-gateway missing %q", want)
		}
	}
}

func TestDashboardGatewayScriptEnsuresBasicAuthBeforeBind(t *testing.T) {
	data, err := os.ReadFile(filepath.Join("rootfs", "usr", "local", "bin", "start-hermes-dashboard-gateway"))
	if err != nil {
		t.Fatalf("read start-hermes-dashboard-gateway: %v", err)
	}
	script := string(data)
	for _, want := range []string{
		"ensure_dashboard_basic_auth",
		"HERMES_DASHBOARD_BASIC_AUTH_USERNAME",
		"HERMES_DASHBOARD_BASIC_AUTH_PASSWORD",
		"CLAWMANAGER_DASHBOARD_BASIC_AUTH_PASSWORD",
		"CLAWMANAGER_INSTANCE_ACCESS_TOKEN",
		"CLAWMANAGER_INSTANCE_TOKEN",
		"CLAWMANAGER_GATEWAY_TOKEN",
		".clawmanager-dashboard-basic-auth",
		`--host "${host}"`,
		`--port "${port}"`,
		"--no-open",
		"--skip-build",
	} {
		if !strings.Contains(script, want) {
			t.Fatalf("start-hermes-dashboard-gateway missing %q", want)
		}
	}
	if idx := strings.Index(script, "hermes dashboard"); idx < 0 {
		t.Fatal("start-hermes-dashboard-gateway missing hermes dashboard launch")
	} else if strings.Contains(script[idx:], "--insecure") {
		t.Fatal("hermes dashboard launch must not pass --insecure; basic auth is required for non-loopback binds")
	}
}

func TestDashboardGatewayScriptStartsClawManagerInstanceAgent(t *testing.T) {
	data, err := os.ReadFile(filepath.Join("rootfs", "usr", "local", "bin", "start-hermes-dashboard-gateway"))
	if err != nil {
		t.Fatalf("read start-hermes-dashboard-gateway: %v", err)
	}
	script := string(data)
	for _, want := range []string{
		"agent_pid",
		"CLAWMANAGER_AGENT_ENABLED",
		"/usr/local/bin/clawmanager-agent",
		"unset RUNTIME_AGENT_CONTROL_TOKEN",
		"unset RUNTIME_AGENT_REPORT_TOKEN",
		"unset RUNTIME_AGENT_DATA_DIR",
		"unset RUNTIME_AGENT_PUBLIC_PORT",
		"unset RUNTIME_AGENT_LISTEN_ADDR",
		`kill "${agent_pid}"`,
		`wait "${agent_pid}"`,
	} {
		if !strings.Contains(script, want) {
			t.Fatalf("start-hermes-dashboard-gateway missing %q", want)
		}
	}
}

func TestApplyRuntimeConfigAliasesClawManagerProviderAsCustom(t *testing.T) {
	data, err := os.ReadFile(filepath.Join("rootfs", "usr", "local", "bin", "hermes-apply-runtime-config"))
	if err != nil {
		t.Fatalf("read hermes-apply-runtime-config: %v", err)
	}
	script := string(data)
	for _, want := range []string{
		`if provider_key == "clawmanager":`,
		`custom_entry = dict(provider_entry)`,
		`custom_entry["name"] = "custom"`,
		`providers_cfg["custom"] = custom_entry`,
	} {
		if !strings.Contains(script, want) {
			t.Fatalf("hermes-apply-runtime-config missing %q", want)
		}
	}
}

func TestApplyRuntimeConfigAppliesScheduledTasks(t *testing.T) {
	data, err := os.ReadFile(filepath.Join("rootfs", "usr", "local", "bin", "hermes-apply-runtime-config"))
	if err != nil {
		t.Fatalf("read hermes-apply-runtime-config: %v", err)
	}
	script := string(data)
	for _, want := range []string{
		`"scheduled_tasks": (`,
		`CLAWMANAGER_HERMES_SCHEDULED_TASKS_JSON`,
		`def apply_scheduled_tasks(hermes_home):`,
		`jobs_path = cron_dir / "jobs.json"`,
		`scheduled_tasks_record = apply_scheduled_tasks(hermes_home)`,
	} {
		if !strings.Contains(script, want) {
			t.Fatalf("hermes-apply-runtime-config missing %q", want)
		}
	}
}

func TestStartHermesGatewayEnsuresDefaultProfileAndProStart(t *testing.T) {
	data, err := os.ReadFile(filepath.Join("rootfs", "usr", "local", "bin", "start-hermes-gateway"))
	if err != nil {
		t.Fatalf("read start-hermes-gateway: %v", err)
	}
	script := string(data)
	for _, want := range []string{
		"ensure_default_gateway_profile",
		"hermes profile create default",
		"has_scheduled_tasks_env",
		"is_hermes_pro_desktop",
		"CLAWMANAGER_HERMES_SCHEDULED_TASKS_JSON",
		"hermes gateway run --accept-hooks --no-supervise",
	} {
		if !strings.Contains(script, want) {
			t.Fatalf("start-hermes-gateway missing %q", want)
		}
	}
	if strings.Contains(script, "exec hermes gateway'") || strings.Contains(script, "exec hermes gateway\"") {
		t.Fatal("start-hermes-gateway must not exec bare `hermes gateway` without run")
	}
	if strings.Contains(script, "&& exec hermes gateway\n") || strings.Contains(script, "&& exec hermes gateway'") {
		t.Fatal("start-hermes-gateway must not exec bare `hermes gateway` without run")
	}
}

func TestDockerfilePinsHermesAgentVersion(t *testing.T) {
	data, err := os.ReadFile("Dockerfile")
	if err != nil {
		t.Fatalf("read Dockerfile: %v", err)
	}
	dockerfile := string(data)
	for _, want := range []string{
		"ARG HERMES_VERSION=0.16.0",
		"ARG HERMES_GIT_REF=v2026.6.5",
		"raw.githubusercontent.com/NousResearch/hermes-agent/${HERMES_GIT_REF}/scripts/install.sh",
		`--branch "${HERMES_GIT_REF}"`,
		`hermes-agent[dingtalk,messaging,matrix,wecom]==${HERMES_VERSION}`,
	} {
		if !strings.Contains(dockerfile, want) {
			t.Fatalf("Dockerfile missing %q", want)
		}
	}
}

func TestDashboardGatewayScriptAppliesRuntimeConfigBeforeReadingEnvFile(t *testing.T) {
	data, err := os.ReadFile(filepath.Join("rootfs", "usr", "local", "bin", "start-hermes-dashboard-gateway"))
	if err != nil {
		t.Fatalf("read start-hermes-dashboard-gateway: %v", err)
	}
	script := string(data)
	applyIndex := strings.Index(script, "/usr/local/bin/hermes-apply-runtime-config")
	envFileIndex := strings.Index(script, `env_file="${HERMES_HOME}/.env"`)
	if applyIndex < 0 {
		t.Fatal("start-hermes-dashboard-gateway missing hermes-apply-runtime-config")
	}
	if envFileIndex < 0 {
		t.Fatal("start-hermes-dashboard-gateway missing HERMES_HOME env file assignment")
	}
	if applyIndex >= envFileIndex {
		t.Fatal("hermes-apply-runtime-config must run before reading HERMES_HOME/.env")
	}
}
