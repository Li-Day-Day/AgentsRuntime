package instanceagent

import (
	"os"
	"path/filepath"
	"testing"
)

func TestRuntimeVersionReadsWorkBuddyMetadataWithoutExecutingRuntime(t *testing.T) {
	root := t.TempDir()
	metadataPath := filepath.Join(root, "build-info.json")
	if err := os.WriteFile(metadataPath, []byte(`{"upstreamVersion":"5.3.8"}`), 0o644); err != nil {
		t.Fatal(err)
	}

	if got := runtimeVersion(filepath.Join(root, "does-not-exist"), metadataPath); got != "5.3.8" {
		t.Fatalf("runtimeVersion() = %q, want 5.3.8", got)
	}
}

func TestMatchesRuntimeProcessRecognizesOnlyWorkBuddyMainProcess(t *testing.T) {
	tests := []struct {
		name string
		argv []string
		want bool
	}{
		{name: "main", argv: []string{"/opt/workbuddy/electron", "--no-sandbox", "--in-process-gpu"}, want: true},
		{name: "version probe", argv: []string{"/opt/workbuddy/electron", "--version"}, want: false},
		{name: "zygote", argv: []string{"/opt/workbuddy/electron", "--type=zygote", "--no-sandbox"}, want: false},
		{name: "daemon", argv: []string{"/opt/workbuddy/electron", "/opt/workbuddy/resources/app.asar/main/daemon-app-server-entry.js", "--stdio"}, want: false},
		{name: "cli", argv: []string{"/opt/workbuddy/electron", "/opt/workbuddy/resources/app.asar.unpacked/cli/bin/codebuddy", "--serve"}, want: false},
		{name: "network service", argv: []string{"/proc/self/exe", "--type=utility"}, want: false},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if got := matchesRuntimeProcess("/opt/workbuddy/electron", test.argv, "electron"); got != test.want {
				t.Fatalf("matchesRuntimeProcess() = %t, want %t", got, test.want)
			}
		})
	}
}

func TestInspectRuntimeDoesNotReportInstalledRuntimeAsRunning(t *testing.T) {
	root := t.TempDir()
	metadataPath := filepath.Join(root, "build-info.json")
	if err := os.WriteFile(metadataPath, []byte(`{"upstreamVersion":"5.3.8"}`), 0o644); err != nil {
		t.Fatal(err)
	}
	status, pid, version := inspectRuntime(filepath.Join(root, "not-running"), metadataPath)
	if status != "error" || pid != 0 || version != "" {
		t.Fatalf("inspectRuntime() = (%q, %d, %q), want (error, 0, empty)", status, pid, version)
	}
}
