package openclaw

import (
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

func TestWriteOpenClawGatewayConfigSeedsInstancePluginRuntime(t *testing.T) {
	root := t.TempDir()
	defaultsRoot := filepath.Join(root, "defaults", ".openclaw")
	feishuSource := filepath.Join(defaultsRoot, "npm", "node_modules", "@openclaw", "feishu")
	if err := os.MkdirAll(feishuSource, 0o755); err != nil {
		t.Fatalf("mkdir default npm plugin: %v", err)
	}
	if err := os.WriteFile(filepath.Join(feishuSource, "index.js"), []byte("image-plugin"), 0o644); err != nil {
		t.Fatalf("write default npm plugin: %v", err)
	}
	extensionSource := filepath.Join(defaultsRoot, "extensions", "sample-channel")
	if err := os.MkdirAll(extensionSource, 0o755); err != nil {
		t.Fatalf("mkdir default extension: %v", err)
	}
	if err := os.WriteFile(filepath.Join(extensionSource, "openclaw.plugin.json"), []byte(`{"id":"sample-channel"}`), 0o644); err != nil {
		t.Fatalf("write default extension: %v", err)
	}
	pluginsSource := filepath.Join(defaultsRoot, "plugins")
	if err := os.MkdirAll(pluginsSource, 0o755); err != nil {
		t.Fatalf("mkdir default plugin registry: %v", err)
	}
	registry := map[string]any{
		"installRecords": map[string]any{
			"feishu": map[string]any{
				"installPath": filepath.ToSlash(feishuSource),
			},
		},
		"plugins": []any{
			map[string]any{
				"pluginId":     "feishu",
				"manifestPath": filepath.ToSlash(filepath.Join(feishuSource, "openclaw.plugin.json")),
			},
		},
	}
	registryData, err := json.Marshal(registry)
	if err != nil {
		t.Fatalf("marshal default plugin registry: %v", err)
	}
	if err := os.WriteFile(filepath.Join(pluginsSource, "installs.json"), registryData, 0o644); err != nil {
		t.Fatalf("write default plugin registry: %v", err)
	}

	linkTarget := "/usr/local/lib/node_modules/openclaw"
	if runtime.GOOS != "windows" {
		if err := os.Symlink(linkTarget, filepath.Join(defaultsRoot, "npm", "node_modules", "openclaw")); err != nil {
			t.Fatalf("create default npm peer symlink: %v", err)
		}
	}

	t.Setenv(openClawDefaultsDirEnv, defaultsRoot)
	workspace := filepath.Join(root, "workspaces", "openclaw", "user-1", "instance-256")
	home := filepath.Join(workspace, "home")
	activeRoot := filepath.Join(home, ".openclaw")
	req := CreateGatewayRequest{
		AgentType: "openclaw", UserID: 1, InstanceID: 256,
		Environment: map[string]string{
			"CLAWMANAGER_WORKSPACE_PATH":       workspace,
			"HOME":                             home,
			"CLAWMANAGER_AGENT_PERSISTENT_DIR": activeRoot,
		},
	}
	if err := WriteGatewayConfig(Config{GatewayAuthMode: "trusted-proxy"}, req, workspace, 20056); err != nil {
		t.Fatalf("WriteGatewayConfig() error = %v", err)
	}

	feishuTarget := filepath.Join(activeRoot, "npm", "node_modules", "@openclaw", "feishu", "index.js")
	if got, err := os.ReadFile(feishuTarget); err != nil {
		t.Fatalf("read copied npm plugin: %v", err)
	} else if string(got) != "image-plugin" {
		t.Fatalf("copied npm plugin = %q, want image content", got)
	}
	if _, err := os.Stat(filepath.Join(activeRoot, "extensions", "sample-channel", "openclaw.plugin.json")); err != nil {
		t.Fatalf("stat copied extension: %v", err)
	}
	if runtime.GOOS != "windows" {
		if got, err := os.Readlink(filepath.Join(activeRoot, "npm", "node_modules", "openclaw")); err != nil {
			t.Fatalf("read copied npm peer symlink: %v", err)
		} else if got != linkTarget {
			t.Fatalf("copied npm peer symlink = %q, want %q", got, linkTarget)
		}
	}

	copiedRegistry, err := os.ReadFile(filepath.Join(activeRoot, "plugins", "installs.json"))
	if err != nil {
		t.Fatalf("read copied plugin registry: %v", err)
	}
	if strings.Contains(string(copiedRegistry), filepath.ToSlash(defaultsRoot)+"/") {
		t.Fatal("copied plugin registry still references defaults root")
	}
	if !strings.Contains(string(copiedRegistry), filepath.ToSlash(activeRoot)+"/") {
		t.Fatal("copied plugin registry does not reference instance plugin root")
	}

	configData, err := os.ReadFile(filepath.Join(activeRoot, "openclaw.json"))
	if err != nil {
		t.Fatalf("read instance config: %v", err)
	}
	var config map[string]any
	if err := json.Unmarshal(configData, &config); err != nil {
		t.Fatalf("parse instance config: %v", err)
	}
	if got, want := objectAt(t, objectAt(t, config, "agents"), "defaults")["workspace"], filepath.ToSlash(filepath.Join(activeRoot, "workspace")); got != want {
		t.Fatalf("agents.defaults.workspace = %#v, want %q", got, want)
	}

	if err := os.WriteFile(feishuTarget, []byte("user-modified"), 0o644); err != nil {
		t.Fatalf("modify instance plugin: %v", err)
	}
	if err := os.WriteFile(filepath.Join(feishuSource, "index.js"), []byte("new-image-plugin"), 0o644); err != nil {
		t.Fatalf("modify default plugin fixture: %v", err)
	}
	if err := WriteGatewayConfig(Config{GatewayAuthMode: "trusted-proxy"}, req, workspace, 20056); err != nil {
		t.Fatalf("second WriteGatewayConfig() error = %v", err)
	}
	if got, err := os.ReadFile(feishuTarget); err != nil {
		t.Fatalf("read user-modified instance plugin: %v", err)
	} else if string(got) != "user-modified" {
		t.Fatalf("instance plugin was overwritten: %q", got)
	}
}

func TestWriteOpenClawGatewayConfigRejectsMismatchedInjectedPersistentDir(t *testing.T) {
	root := t.TempDir()
	workspace := filepath.Join(root, "workspaces", "openclaw", "user-1", "instance-256")
	req := CreateGatewayRequest{
		AgentType: "openclaw", UserID: 1, InstanceID: 256,
		Environment: map[string]string{
			"CLAWMANAGER_WORKSPACE_PATH":       workspace,
			"HOME":                             filepath.Join(workspace, "home"),
			"CLAWMANAGER_AGENT_PERSISTENT_DIR": filepath.Join(root, "other-instance", ".openclaw"),
		},
	}
	err := WriteGatewayConfig(Config{GatewayAuthMode: "trusted-proxy"}, req, workspace, 20056)
	if err == nil || !strings.Contains(err.Error(), "CLAWMANAGER_AGENT_PERSISTENT_DIR") {
		t.Fatalf("WriteGatewayConfig() error = %v, want mismatched persistent dir error", err)
	}
}

func TestWriteOpenClawGatewayConfigRepairsMissingDingTalkOpenClawPeer(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("creating symlinks requires elevated Windows privileges")
	}

	root := t.TempDir()
	globalPackage := filepath.Join(root, "global", "openclaw")
	if err := os.MkdirAll(globalPackage, 0o755); err != nil {
		t.Fatalf("mkdir global OpenClaw package: %v", err)
	}
	previousGlobalPackage := openClawGlobalPackageDir
	openClawGlobalPackageDir = globalPackage
	t.Cleanup(func() { openClawGlobalPackageDir = previousGlobalPackage })

	defaultsRoot := filepath.Join(root, "defaults", ".openclaw")
	connectorSource := filepath.Join(defaultsRoot, "npm", "node_modules", "@dingtalk-real-ai", "dingtalk-connector")
	if err := os.MkdirAll(connectorSource, 0o755); err != nil {
		t.Fatalf("mkdir default DingTalk connector: %v", err)
	}
	t.Setenv(openClawDefaultsDirEnv, defaultsRoot)

	workspace := filepath.Join(root, "workspaces", "openclaw", "user-1", "instance-350")
	activeRoot := filepath.Join(workspace, "home", ".openclaw")
	// Simulate a previously seeded instance whose npm directory exists but
	// whose OpenClaw peer link was omitted.
	connectorTarget := filepath.Join(activeRoot, "npm", "node_modules", "@dingtalk-real-ai", "dingtalk-connector")
	if err := os.MkdirAll(connectorTarget, 0o755); err != nil {
		t.Fatalf("mkdir instance DingTalk connector: %v", err)
	}

	req := CreateGatewayRequest{AgentType: "openclaw", UserID: 1, InstanceID: 350}
	if err := WriteGatewayConfig(Config{GatewayAuthMode: "trusted-proxy"}, req, workspace, 20009); err != nil {
		t.Fatalf("WriteGatewayConfig() error = %v", err)
	}

	linkPath := filepath.Join(activeRoot, "npm", "node_modules", "openclaw")
	if got, err := os.Readlink(linkPath); err != nil {
		t.Fatalf("read repaired OpenClaw peer symlink: %v", err)
	} else if got != globalPackage {
		t.Fatalf("OpenClaw peer symlink = %q, want %q", got, globalPackage)
	}

	if err := WriteGatewayConfig(Config{GatewayAuthMode: "trusted-proxy"}, req, workspace, 20009); err != nil {
		t.Fatalf("second WriteGatewayConfig() error = %v", err)
	}
	if got, err := os.Readlink(linkPath); err != nil {
		t.Fatalf("read idempotent OpenClaw peer symlink: %v", err)
	} else if got != globalPackage {
		t.Fatalf("idempotent OpenClaw peer symlink = %q, want %q", got, globalPackage)
	}
}
