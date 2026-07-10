package gateway

import (
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

const teamSharedWorkspaceMode fs.FileMode = fs.ModeSetgid | 0o775

func PrepareLiteTeamSharedWorkspace(workspaceRoot string, req CreateGatewayRequest, workspacePath string) error {
	_, sharedDir, ok := LiteTeamEnvironment(req, workspacePath)
	if !ok {
		return nil
	}
	sharedDir = filepath.Clean(filepath.FromSlash(sharedDir))
	if !filepath.IsAbs(sharedDir) {
		return fmt.Errorf("%s must be absolute: %s", teamSharedDirEnv, sharedDir)
	}
	if err := validateLiteTeamSharedPath(workspaceRoot, req, workspacePath, sharedDir); err != nil {
		return err
	}
	if info, err := os.Lstat(sharedDir); err == nil && info.Mode()&os.ModeSymlink != 0 {
		return fmt.Errorf("Team shared workspace root may not be a symlink: %s", sharedDir)
	} else if err != nil && !os.IsNotExist(err) {
		return err
	}
	sharedGID := liteTeamSharedGID(req)
	if err := os.MkdirAll(sharedDir, teamSharedWorkspaceMode); err != nil {
		return fmt.Errorf("create lite Team shared workspace: %w", err)
	}
	if err := repairLiteTeamSharedTree(sharedDir, sharedGID); err != nil {
		return err
	}
	return ensureLiteTeamWorkspaceAlias(workspacePath, sharedDir, req)
}

func validateLiteTeamSharedPath(workspaceRoot string, req CreateGatewayRequest, workspacePath, sharedDir string) error {
	workspacePath = filepath.Clean(workspacePath)
	if sharedDir == filepath.Join(workspacePath, "team") {
		return nil
	}
	teamID, ok := requestEnvValue(req, "CLAWMANAGER_TEAM_ID")
	teamID = strings.TrimSpace(teamID)
	if !ok || teamID == "" || strings.ContainsAny(teamID, `/\\`) || teamID == "." || teamID == ".." {
		return fmt.Errorf("valid CLAWMANAGER_TEAM_ID is required for external Team shared workspace")
	}
	root := filepath.Clean(workspaceRoot)
	if root == "." || root == "" {
		root = filepath.Dir(filepath.Dir(filepath.Dir(workspacePath)))
	}
	expected := filepath.Join(root, "teams", "user-"+strconv.Itoa(req.UserID), "team-"+teamID+"-shared")
	if sharedDir != filepath.Clean(expected) {
		return fmt.Errorf("Team shared workspace escaped current Team scope: got %s want %s", sharedDir, expected)
	}
	return nil
}

func repairLiteTeamSharedTree(root string, gid int) error {
	return filepath.WalkDir(root, func(current string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if entry.Type()&os.ModeSymlink != 0 {
			return nil
		}
		if err := ChgrpWorkspace(current, gid); err != nil {
			return fmt.Errorf("set Team shared group on %s: %w", current, err)
		}
		info, err := entry.Info()
		if err != nil {
			return err
		}
		if info.IsDir() {
			mode := teamSharedWorkspaceMode | info.Mode().Perm() | 0o770
			if err := os.Chmod(current, mode); err != nil {
				return fmt.Errorf("repair Team shared directory %s: %w", current, err)
			}
			return nil
		}
		mode := info.Mode().Perm() | 0o660
		if err := os.Chmod(current, mode); err != nil {
			return fmt.Errorf("repair Team shared file %s: %w", current, err)
		}
		return nil
	})
}

func ensureLiteTeamWorkspaceAlias(workspacePath, sharedDir string, req CreateGatewayRequest) error {
	aliasParent := filepath.Join(workspacePath, "home", ".openclaw", "workspace")
	if err := os.MkdirAll(aliasParent, 0o750); err != nil {
		return fmt.Errorf("create Team workspace alias parent: %w", err)
	}
	if err := ChownWorkspace(aliasParent, req.UID, req.GID); err != nil {
		return fmt.Errorf("chown Team workspace alias parent: %w", err)
	}
	alias := filepath.Join(aliasParent, "team")
	info, err := os.Lstat(alias)
	if err == nil {
		if info.Mode()&os.ModeSymlink != 0 {
			target, readErr := os.Readlink(alias)
			if readErr != nil {
				return readErr
			}
			if filepath.Clean(target) != sharedDir {
				return fmt.Errorf("Team workspace alias already points to a different Team: %s", target)
			}
			return nil
		}
		if !info.IsDir() {
			return fmt.Errorf("Team workspace alias path is not a directory or symlink: %s", alias)
		}
		entries, readErr := os.ReadDir(alias)
		if readErr != nil {
			return readErr
		}
		if len(entries) == 0 {
			if removeErr := os.Remove(alias); removeErr != nil {
				return removeErr
			}
		} else {
			memberID, _ := requestEnvValue(req, "CLAWMANAGER_TEAM_MEMBER_ID")
			backup := alias + ".private-backup-" + safeWorkspaceComponent(memberID)
			if _, backupErr := os.Lstat(backup); backupErr == nil {
				return fmt.Errorf("Team private workspace backup already exists: %s", backup)
			} else if !os.IsNotExist(backupErr) {
				return backupErr
			}
			if renameErr := os.Rename(alias, backup); renameErr != nil {
				return fmt.Errorf("preserve existing private Team workspace: %w", renameErr)
			}
		}
	} else if !os.IsNotExist(err) {
		return err
	}
	if err := os.Symlink(sharedDir, alias); err != nil {
		return fmt.Errorf("create per-instance Team workspace alias: %w", err)
	}
	return nil
}

func safeWorkspaceComponent(value string) string {
	var out strings.Builder
	for _, r := range strings.TrimSpace(value) {
		if r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z' || r >= '0' && r <= '9' || r == '-' || r == '_' {
			out.WriteRune(r)
		}
	}
	if out.Len() == 0 {
		return "member"
	}
	return out.String()
}
