package openclaw_test

import (
	"os"
	"strings"
	"testing"
)

func TestImageKeepsBundledPluginsReadableAndRootOwned(t *testing.T) {
	content, err := os.ReadFile("Dockerfile.openclaw")
	if err != nil {
		t.Fatalf("read Dockerfile.openclaw: %v", err)
	}
	dockerfile := string(content)

	required := []string{
		"chmod 0755 /defaults /defaults/.openclaw",
		"chmod -R a+rX /defaults/.openclaw/npm",
		"mkdir -p /defaults/.openclaw/npm/node_modules",
		"ln -s /usr/local/lib/node_modules/openclaw /defaults/.openclaw/npm/node_modules/openclaw",
		"find /defaults/.openclaw/npm/projects -path \"*/node_modules/${plugin_package}\"",
		"chown -R root:root \"${package_dir}\"",
		"legacy_dir=\"/defaults/.openclaw/npm/node_modules/${plugin_package}\"",
		"chown -h root:root /defaults/.openclaw/npm/node_modules/openclaw",
	}
	for _, fragment := range required {
		if !strings.Contains(dockerfile, fragment) {
			t.Errorf("Dockerfile.openclaw is missing %q", fragment)
		}
	}

	abcOwnership := strings.Index(dockerfile, "chown -R abc:abc /defaults/.openclaw")
	rootOwnership := strings.Index(dockerfile, "chown -R root:root")
	if abcOwnership < 0 || rootOwnership < 0 || rootOwnership < abcOwnership {
		t.Error("bundled plugin root ownership must be applied after the default abc ownership")
	}

	runScript, err := os.ReadFile("../clawmanager-agent/scripts/clawmanager-agent-run")
	if err != nil {
		t.Fatalf("read clawmanager-agent-run: %v", err)
	}
	script := string(runScript)
	for _, fragment := range required[:2] {
		if !strings.Contains(script, fragment) {
			t.Errorf("clawmanager-agent-run is missing %q", fragment)
		}
	}
}
