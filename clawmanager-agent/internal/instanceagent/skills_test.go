package instanceagent

import (
	"archive/zip"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
)

func TestContentMD5MatchesSpecReference(t *testing.T) {
	root := t.TempDir()
	mustWriteFile(t, filepath.Join(root, "skill.json"), []byte("{\"id\":\"weather\"}\n"))
	mustMkdir(t, filepath.Join(root, "src"))
	mustWriteFile(t, filepath.Join(root, "src", "main.py"), []byte("print(\"hi\")\n"))
	mustMkdir(t, filepath.Join(root, "empty"))
	mustMkdir(t, filepath.Join(root, "__pycache__"))
	mustWriteFile(t, filepath.Join(root, "__pycache__", "cache.pyc"), []byte("cache"))
	mustMkdir(t, filepath.Join(root, "node_modules"))
	mustWriteFile(t, filepath.Join(root, "node_modules", "pkg.js"), []byte("pkg"))
	mustMkdir(t, filepath.Join(root, ".git"))
	mustWriteFile(t, filepath.Join(root, ".git", "HEAD"), []byte("ignored"))

	got, sizeBytes, fileCount, err := ContentMD5(root)
	if err != nil {
		t.Fatal(err)
	}

	if got != "fb5a5452093efad725fa05154472f4d0" {
		t.Fatalf("content md5 mismatch: %s", got)
	}
	if fileCount != 4 {
		t.Fatalf("expected 4 participating files, got %d", fileCount)
	}
	wantSize := int64(len("{\"id\":\"weather\"}\n") + len("print(\"hi\")\n") + len("cache") + len("pkg"))
	if sizeBytes != wantSize {
		t.Fatalf("expected participating size %d, got %d", wantSize, sizeBytes)
	}
}

func TestContentMD5IgnoresHiddenSegments(t *testing.T) {
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "main.py"), []byte("print('hello')\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	hashA, sizeA, filesA, err := ContentMD5(root)
	if err != nil {
		t.Fatal(err)
	}

	if err := os.Mkdir(filepath.Join(root, ".git"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, ".git", "HEAD"), []byte("ignored\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, ".hidden"), []byte("ignored\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(filepath.Join(root, "empty"), 0o755); err != nil {
		t.Fatal(err)
	}

	hashB, sizeB, filesB, err := ContentMD5(root)
	if err != nil {
		t.Fatal(err)
	}

	if hashA != hashB {
		t.Fatalf("hash changed after hidden files: %s != %s", hashA, hashB)
	}
	if sizeA != sizeB || filesA != filesB {
		t.Fatalf("hidden files affected stats: size %d/%d files %d/%d", sizeA, sizeB, filesA, filesB)
	}
}

func TestCreateSkillPackageUsesSpecContentWithSingleTopDirectory(t *testing.T) {
	root := t.TempDir()
	skillDir := filepath.Join(root, "weather")
	mustMkdir(t, skillDir)
	mustWriteFile(t, filepath.Join(skillDir, "skill.json"), []byte("{\"id\":\"weather\"}\n"))
	mustMkdir(t, filepath.Join(skillDir, "src"))
	mustWriteFile(t, filepath.Join(skillDir, "src", "main.py"), []byte("print(\"hi\")\n"))
	mustMkdir(t, filepath.Join(skillDir, "empty"))
	mustMkdir(t, filepath.Join(skillDir, "__pycache__"))
	mustWriteFile(t, filepath.Join(skillDir, "__pycache__", "cache.pyc"), []byte("cache"))
	mustMkdir(t, filepath.Join(skillDir, ".cache"))
	mustWriteFile(t, filepath.Join(skillDir, ".cache", "ignored"), []byte("ignored"))

	zipPath, cleanup, err := CreateSkillPackage(SkillInfo{Identifier: "weather", InstallPath: skillDir})
	if cleanup != nil {
		defer cleanup()
	}
	if err != nil {
		t.Fatal(err)
	}

	reader, err := zip.OpenReader(zipPath)
	if err != nil {
		t.Fatal(err)
	}
	defer reader.Close()

	var names []string
	for _, file := range reader.File {
		names = append(names, file.Name)
	}
	sort.Strings(names)
	got := strings.Join(names, "\n")

	expected := []string{
		"weather/",
		"weather/__pycache__/",
		"weather/__pycache__/cache.pyc",
		"weather/skill.json",
		"weather/src/",
		"weather/src/main.py",
	}
	if got != strings.Join(expected, "\n") {
		t.Fatalf("unexpected zip entries:\n%s", got)
	}
	for _, name := range names {
		if strings.Contains(name, "/.cache/") || strings.Contains(name, "/empty/") {
			t.Fatalf("zip included non-participating entry %q", name)
		}
	}
}

func TestScanSkillsReadsSkillJSON(t *testing.T) {
	root := t.TempDir()
	skillDir := filepath.Join(root, "weather")
	if err := os.Mkdir(skillDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(skillDir, "skill.json"), []byte(`{"id":"hermes-weather","version":"1.2.0","name":"weather"}`), 0o644); err != nil {
		t.Fatal(err)
	}

	skills, err := ScanSkills([]string{root})
	if err != nil {
		t.Fatal(err)
	}
	if len(skills) != 1 {
		t.Fatalf("expected 1 skill, got %d", len(skills))
	}
	if skills[0].SkillID != "hermes-weather" || skills[0].SkillVersion != "1.2.0" || skills[0].Identifier != "weather" {
		t.Fatalf("unexpected skill metadata: %+v", skills[0])
	}
}

func mustMkdir(t *testing.T, path string) {
	t.Helper()
	if err := os.Mkdir(path, 0o755); err != nil {
		t.Fatal(err)
	}
}

func mustWriteFile(t *testing.T, path string, data []byte) {
	t.Helper()
	if err := os.WriteFile(path, data, 0o644); err != nil {
		t.Fatal(err)
	}
}
