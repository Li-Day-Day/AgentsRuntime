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
		"HERMES_TEAM_WORKER_PORT",
		"HERMES_TEAM_WORKER_HOME",
		`.clawmanager-team-worker`,
		`export HERMES_HOME="${team_worker_home}/.hermes"`,
		`export CLAWMANAGER_GATEWAY_PORT="${team_worker_port}"`,
		`CLAWMANAGER_TEAM_READY_FILE`,
		`HERMES_TEAM_STARTUP_TIMEOUT_SECONDS`,
		`[ "${team_worker_port}" -eq "${port}" ]`,
		`[ "${team_worker_port}" -gt 65535 ]`,
		`/usr/local/bin/hermes-apply-runtime-config`,
		`for managed_identity in SOUL.md AGENTS.md team.json team-introduction.md`,
		"hermes gateway run --accept-hooks --no-supervise",
		`team_failure_file="${team_ready_file}.failed"`,
		`grep -Eq '"ready"[[:space:]]*:[[:space:]]*true' "${team_ready_file}"`,
		`Hermes Redis Team consumer reported a non-retryable startup failure`,
		`wait -n "${wait_pids[@]}"`,
	} {
		if !strings.Contains(script, want) {
			t.Fatalf("start-hermes-dashboard-gateway missing %q", want)
		}
	}
	teamStart := strings.LastIndex(script, "start_team_gateway")
	dashboardStart := strings.LastIndex(script, `echo "Starting Hermes dashboard gateway`)
	if dashboardStart < 0 || teamStart < 0 || teamStart > dashboardStart {
		t.Fatalf("Team consumer readiness must be established before the dashboard becomes healthy")
	}
}

func TestDockerfilePackagesCanonicalRedisTeamAdapter(t *testing.T) {
	data, err := os.ReadFile("Dockerfile")
	if err != nil {
		t.Fatalf("read Dockerfile: %v", err)
	}
	dockerfile := string(data)
	if !strings.Contains(dockerfile, "COPY plugins/hermes-redis-team/ /tmp/hermes-vendor-plugins/redis_team/") {
		t.Fatal("Dockerfile does not package the canonical Hermes Redis Team adapter")
	}
	if strings.Contains(dockerfile, "COPY hermes/vendor-plugins/redis_team/") {
		t.Fatal("Dockerfile still packages the stale vendor mirror")
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
