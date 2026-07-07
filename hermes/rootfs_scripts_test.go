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
		"HERMES_TEAM_WORKER_HOME",
		"CLAWMANAGER_TEAM_WORKER_PORT",
		"hermes-apply-runtime-config",
		"dashboard_port=${port}",
		"team_worker_port=${team_worker_port}",
		"dashboard_pid=${dashboard_pid}",
		"team_gateway_pid=${team_gateway_pid}",
		"hermes gateway run --accept-hooks --no-supervise",
	} {
		if !strings.Contains(script, want) {
			t.Fatalf("start-hermes-dashboard-gateway missing %q", want)
		}
	}
	if !strings.Contains(script, `export HOME="${team_worker_home}"`) ||
		!strings.Contains(script, `export PORT="${team_worker_port}"`) {
		t.Fatal("start-hermes-dashboard-gateway must isolate the Redis Team consumer from the dashboard HOME and port")
	}
	dashboardIndex := strings.Index(script, "hermes dashboard")
	teamIndex := strings.Index(script, "Starting Hermes Redis Team consumer")
	if dashboardIndex < 0 || teamIndex < 0 || dashboardIndex > teamIndex {
		t.Fatal("start-hermes-dashboard-gateway must bind the dashboard port before starting the Redis Team consumer")
	}
}

func TestHermesGatewayScriptUsesExplicitRunCommandForRootLaunch(t *testing.T) {
	data, err := os.ReadFile(filepath.Join("rootfs", "usr", "local", "bin", "start-hermes-gateway"))
	if err != nil {
		t.Fatalf("read start-hermes-gateway: %v", err)
	}
	script := string(data)
	if !strings.Contains(script, "exec hermes gateway run --accept-hooks --no-supervise") {
		t.Fatal("start-hermes-gateway must use explicit gateway run command so Redis Team-only Pro runtimes do not look for a default gateway profile")
	}
	if strings.Contains(script, "exec hermes gateway'\n") {
		t.Fatal("start-hermes-gateway must not launch the bare default gateway profile in the root/s6 branch")
	}
}

func TestRedisTeamProtocolUsesExplicitCompletionAndCanonicalBuildInputs(t *testing.T) {
	adapterData, err := os.ReadFile(filepath.Join("..", "plugins", "hermes-redis-team", "adapter.py"))
	if err != nil {
		t.Fatalf("read canonical Hermes Redis Team adapter: %v", err)
	}
	adapter := string(adapterData)
	for _, want := range []string{
		"WIRE_SCHEMA_VERSION = 1",
		"PROTOCOL_VERSION = 2",
		"Agent turn finished; waiting for explicit team_complete_task",
		"\"task_progress\"",
		"\"completionSource\": COMPLETION_SOURCE",
		"\"explicitCompletion\": True",
		"xadd_terminal_once",
		"completion_key",
		"processed_message_key",
		"_stable_assignment_id",
		"assignment-",
		"validate_artifact_refs",
		"write_task_envelope",
		"_atomic_write_text(result_md, result_markdown)",
		"if task_result_is_terminal(self.settings, task_id):",
	} {
		if !strings.Contains(adapter, want) {
			t.Fatalf("canonical Hermes Redis Team adapter missing %q", want)
		}
	}
	if strings.Contains(adapter, "self._seen_ids") {
		t.Fatal("Hermes Redis Team adapter must use Redis-backed message idempotency instead of process memory")
	}

	dockerData, err := os.ReadFile("Dockerfile")
	if err != nil {
		t.Fatalf("read Hermes Dockerfile: %v", err)
	}
	if !strings.Contains(string(dockerData), "COPY plugins/hermes-redis-team/ /tmp/hermes-vendor-plugins/redis_team/") {
		t.Fatal("Hermes image must package the canonical Redis Team plugin source")
	}

	migrationData, err := os.ReadFile(filepath.Join("rootfs", "usr", "local", "bin", "hermes-apply-runtime-config"))
	if err != nil {
		t.Fatalf("read Hermes runtime config migration: %v", err)
	}
	migration := string(migrationData)
	for _, want := range []string{"hashlib.sha256", "source_sha256", "previous_sha256"} {
		if !strings.Contains(migration, want) {
			t.Fatalf("Hermes Skill migration missing hash-based refresh token %q", want)
		}
	}
}
