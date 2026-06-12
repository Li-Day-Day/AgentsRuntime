package instanceagent

import (
	"archive/zip"
	"crypto/md5"
	"encoding/hex"
	"encoding/json"
	"errors"
	"hash"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

type hashEntry struct {
	path string
	kind string
	data []byte
}

type skillContent struct {
	files     map[string][]byte
	dirs      map[string]struct{}
	sizeBytes int64
	fileCount int
}

func ScanSkills(dirs []string) ([]SkillInfo, error) {
	var skills []SkillInfo
	seen := map[string]bool{}

	for _, dir := range dirs {
		entries, err := os.ReadDir(dir)
		if err != nil {
			if os.IsNotExist(err) {
				continue
			}
			return nil, err
		}
		for _, entry := range entries {
			if !entry.IsDir() || isHiddenSegment(entry.Name()) {
				continue
			}
			path := filepath.Join(dir, entry.Name())
			if seen[path] {
				continue
			}
			seen[path] = true
			skill, err := InspectSkill(path)
			if err != nil {
				continue
			}
			skills = append(skills, skill)
		}
	}

	sort.Slice(skills, func(i, j int) bool {
		return skills[i].Identifier < skills[j].Identifier
	})
	return skills, nil
}

func InspectSkill(path string) (SkillInfo, error) {
	info, err := os.Stat(path)
	if err != nil {
		return SkillInfo{}, err
	}
	if !info.IsDir() {
		return SkillInfo{}, errors.New("skill path is not a directory")
	}

	manifestName, manifest := readSkillManifest(path)
	contentMD5, sizeBytes, fileCount, err := ContentMD5(path)
	if err != nil {
		return SkillInfo{}, err
	}

	identifier := manifestString(manifest, "identifier", "name", "id")
	if identifier == "" {
		identifier = filepath.Base(path)
	}
	skillID := manifestString(manifest, "skill_id", "id", "identifier", "name")
	if skillID == "" {
		skillID = identifier
	}
	version := manifestString(manifest, "skill_version", "version")
	if version == "" {
		version = "0.0.0"
	}
	source := manifestString(manifest, "source")
	if source == "" {
		source = "discovered_in_instance"
	}

	return SkillInfo{
		SkillID:      skillID,
		SkillVersion: version,
		Identifier:   identifier,
		InstallPath:  path,
		ContentMD5:   contentMD5,
		Source:       source,
		Type:         "hermes-skill",
		SizeBytes:    sizeBytes,
		FileCount:    fileCount,
		Metadata: map[string]any{
			"runtime":  "hermes",
			"manifest": manifestName,
		},
	}, nil
}

func ContentMD5(root string) (string, int64, int, error) {
	content, err := collectSkillContent(root)
	if err != nil {
		return "", 0, 0, err
	}

	entries := content.hashEntries()
	hasher := md5.New()
	for _, entry := range entries {
		writeHashEntry(hasher, entry)
	}
	return hex.EncodeToString(hasher.Sum(nil)), content.sizeBytes, content.fileCount, nil
}

func collectSkillContent(root string) (skillContent, error) {
	content := skillContent{
		files: map[string][]byte{},
		dirs:  map[string]struct{}{},
	}

	err := filepath.WalkDir(root, func(path string, entry os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if path == root {
			return nil
		}

		rel, ok, err := normalizedSkillRel(root, path)
		if err != nil {
			return err
		}
		if !ok {
			if entry.IsDir() {
				return filepath.SkipDir
			}
			return nil
		}

		if entry.IsDir() {
			return nil
		}

		info, err := entry.Info()
		if err != nil {
			return err
		}
		if !info.Mode().IsRegular() {
			return nil
		}
		data, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		content.files[rel] = data
		content.sizeBytes += info.Size()
		content.fileCount++
		addParentDirs(content.dirs, rel)
		return nil
	})
	if err != nil {
		return skillContent{}, err
	}
	return content, nil
}

func (content skillContent) hashEntries() []hashEntry {
	entries := make([]hashEntry, 0, len(content.dirs)+len(content.files))
	for rel := range content.dirs {
		entries = append(entries, hashEntry{path: rel, kind: "dir"})
	}
	for rel, data := range content.files {
		entries = append(entries, hashEntry{path: rel, kind: "file", data: data})
	}

	sort.Slice(entries, func(i, j int) bool {
		if entries[i].path == entries[j].path {
			return entries[i].kind < entries[j].kind
		}
		return entries[i].path < entries[j].path
	})
	return entries
}

func addParentDirs(dirs map[string]struct{}, rel string) {
	parts := strings.Split(rel, "/")
	for i := 1; i < len(parts); i++ {
		parent := strings.Join(parts[:i], "/")
		if parent != "" && !isHiddenRelativePath(parent) {
			dirs[parent] = struct{}{}
		}
	}
}

func writeHashEntry(hasher hash.Hash, entry hashEntry) {
	_, _ = hasher.Write([]byte(entry.path + "\n" + entry.kind + "\n"))
	if entry.kind == "file" {
		_, _ = hasher.Write(entry.data)
		_, _ = hasher.Write([]byte("\n"))
	}
}

func CreateSkillPackage(skill SkillInfo) (string, func(), error) {
	tmp, err := os.CreateTemp("", "hermes-skill-*.zip")
	if err != nil {
		return "", nil, err
	}
	zipPath := tmp.Name()
	cleanup := func() { _ = os.Remove(zipPath) }

	content, err := collectSkillContent(skill.InstallPath)
	if err != nil {
		_ = tmp.Close()
		cleanup()
		return "", nil, err
	}

	writer := zip.NewWriter(tmp)
	top := filepath.Base(skill.InstallPath)
	if top == "." || top == string(filepath.Separator) || top == "" {
		top = skill.Identifier
	}

	_, err = writer.Create(filepath.ToSlash(top) + "/")
	if err == nil {
		for _, dir := range sortedKeys(content.dirs) {
			_, err = writer.Create(filepath.ToSlash(top+"/"+dir) + "/")
			if err != nil {
				break
			}
		}
	}
	if err == nil {
		for _, rel := range sortedKeys(content.files) {
			part, createErr := writer.Create(filepath.ToSlash(top + "/" + rel))
			if createErr != nil {
				err = createErr
				break
			}
			if _, createErr = part.Write(content.files[rel]); createErr != nil {
				err = createErr
				break
			}
		}
	}
	if closeErr := writer.Close(); err == nil {
		err = closeErr
	}
	if closeErr := tmp.Close(); err == nil {
		err = closeErr
	}
	if err != nil {
		cleanup()
		return "", nil, err
	}
	return zipPath, cleanup, nil
}

func sortedKeys[V any](items map[string]V) []string {
	keys := make([]string, 0, len(items))
	for key := range items {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	return keys
}

func normalizedSkillRel(root, path string) (string, bool, error) {
	rel, err := filepath.Rel(root, path)
	if err != nil {
		return "", false, err
	}
	rel = filepath.ToSlash(filepath.Clean(rel))
	rel = strings.TrimPrefix(rel, "./")
	if rel == "" || rel == "." || rel == ".." || strings.HasPrefix(rel, "../") || hasDotDotSegment(rel) || isHiddenRelativePath(rel) {
		return "", false, nil
	}
	return rel, true, nil
}

func hasDotDotSegment(rel string) bool {
	for _, part := range strings.Split(rel, "/") {
		if part == ".." {
			return true
		}
	}
	return false
}

func isHiddenRelativePath(rel string) bool {
	for _, part := range strings.Split(rel, "/") {
		if isHiddenSegment(part) {
			return true
		}
	}
	return false
}

func isHiddenSegment(name string) bool {
	return strings.HasPrefix(name, ".")
}

func readSkillManifest(path string) (string, map[string]any) {
	for _, name := range []string{"skill.json", "manifest.json"} {
		data, err := os.ReadFile(filepath.Join(path, name))
		if err != nil {
			continue
		}
		var manifest map[string]any
		if json.Unmarshal(data, &manifest) == nil {
			return name, manifest
		}
	}
	if _, err := os.Stat(filepath.Join(path, "SKILL.md")); err == nil {
		return "SKILL.md", map[string]any{}
	}
	return "", map[string]any{}
}

func manifestString(manifest map[string]any, keys ...string) string {
	for _, key := range keys {
		if value, ok := manifest[key]; ok && value != nil {
			if text := strings.TrimSpace(strings.Trim(strings.TrimSpace(toString(value)), `"`)); text != "" {
				return text
			}
		}
	}
	return ""
}

func toString(value any) string {
	switch typed := value.(type) {
	case string:
		return typed
	default:
		data, _ := json.Marshal(typed)
		return string(data)
	}
}
