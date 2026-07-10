package gateway

import (
	"os"
	"path/filepath"
	"reflect"
	"testing"
)

func TestLiteTeamGatewayCommandOnlyWrapsTeamGateway(t *testing.T) {
	command := []string{"openclaw", "gateway", "run"}
	if got := LiteTeamGatewayCommand(command, []string{"CLAWMANAGER_TEAM_ENABLED=false"}); !reflect.DeepEqual(got, command) {
		t.Fatalf("non-Team command changed: %#v", got)
	}

	got := LiteTeamGatewayCommand(command, []string{
		"CLAWMANAGER_TEAM_ENABLED=true",
		"CLAWMANAGER_TEAM_UMASK=0002",
	})
	wantPrefix := []string{"/bin/sh", "-c", `umask "$1"; shift; exec "$@"`, "clawmanager-team-gateway", "0002"}
	if len(got) != len(wantPrefix)+len(command) || !reflect.DeepEqual(got[:len(wantPrefix)], wantPrefix) || !reflect.DeepEqual(got[len(wantPrefix):], command) {
		t.Fatalf("Team command = %#v", got)
	}
}

func TestLiteTeamGatewayCommandRejectsInvalidUmask(t *testing.T) {
	got := LiteTeamGatewayCommand([]string{"gateway"}, []string{
		"CLAWMANAGER_TEAM_ENABLED=true",
		"CLAWMANAGER_TEAM_UMASK=0022; touch /tmp/escaped",
	})
	if got[4] != "0002" {
		t.Fatalf("invalid Team umask was not replaced: %#v", got)
	}
}

func TestLiteTeamEnvironmentRemapsGlobalConfigPath(t *testing.T) {
	workspace := filepath.Join(t.TempDir(), "openclaw", "user-1", "instance-2")
	shared := filepath.Join(t.TempDir(), "teams", "user-1", "team-54-shared")
	req := CreateGatewayRequest{Environment: map[string]string{
		"CLAWMANAGER_TEAM_ENABLED":     "true",
		"CLAWMANAGER_TEAM_CONFIG_JSON": `{ "teamId": "54" }`,
		"CLAWMANAGER_TEAM_CONFIG_PATH": "/etc/clawmanager/team/team.json",
		"CLAWMANAGER_TEAM_SHARED_DIR":  shared,
	}}
	configPath, sharedDir, ok := LiteTeamEnvironment(req, workspace)
	if !ok {
		t.Fatal("expected Team environment")
	}
	if sharedDir != shared {
		t.Fatalf("sharedDir = %q want %q", sharedDir, shared)
	}
	if configPath != filepath.Join(shared, "team.json") {
		t.Fatalf("configPath = %q", configPath)
	}
}

func TestWriteLiteTeamConfigRejectsEscapedPathBeforeWriting(t *testing.T) {
	root := t.TempDir()
	shared := filepath.Join(root, "teams", "user-1", "team-54-shared")
	escaped := filepath.Join(root, "outside", "team.json")
	req := CreateGatewayRequest{Environment: map[string]string{
		"CLAWMANAGER_TEAM_ENABLED":     "true",
		"CLAWMANAGER_TEAM_CONFIG_JSON": `{ "teamId": "54" }`,
		"CLAWMANAGER_TEAM_CONFIG_PATH": escaped,
		"CLAWMANAGER_TEAM_SHARED_DIR":  shared,
	}}
	if err := WriteLiteTeamConfigJSON(req, filepath.Join(root, "openclaw", "user-1", "instance-2")); err == nil {
		t.Fatal("expected escaped Team config path to be rejected")
	}
	if _, err := os.Stat(escaped); !os.IsNotExist(err) {
		t.Fatalf("escaped Team config was written before validation: %v", err)
	}
}

func TestPrepareLiteTeamSharedWorkspaceRejectsOtherTeamPath(t *testing.T) {
	root := t.TempDir()
	workspace := filepath.Join(root, "openclaw", "user-1", "instance-2")
	req := CreateGatewayRequest{UserID: 1, Environment: map[string]string{
		"CLAWMANAGER_TEAM_ENABLED":    "true",
		"CLAWMANAGER_TEAM_ID":         "54",
		"CLAWMANAGER_TEAM_MEMBER_ID":  "pm",
		"CLAWMANAGER_TEAM_SHARED_DIR": filepath.Join(root, "teams", "user-1", "team-53-shared"),
	}}
	if err := PrepareLiteTeamSharedWorkspace(root, req, workspace); err == nil {
		t.Fatal("expected cross-Team shared path to be rejected")
	}
}
