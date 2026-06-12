package gateway

import (
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

func ValidateWorkspacePath(root, runtimeType string, req CreateGatewayRequest) (string, error) {
	root = filepath.Clean(root)
	expected := filepath.Join(
		root,
		runtimeType,
		"user-"+strconv.Itoa(req.UserID),
		"instance-"+strconv.Itoa(req.InstanceID),
	)
	requested := filepath.Clean(req.WorkspacePath)
	if requested != expected {
		return "", fmt.Errorf("%w: got %s want %s", ErrWorkspacePath, requested, expected)
	}
	if !pathWithin(root, requested) {
		return "", fmt.Errorf("%w: %s is outside %s", ErrWorkspacePath, requested, root)
	}
	return requested, nil
}

func PrepareWorkspace(root, runtimeType string, req CreateGatewayRequest) (string, error) {
	workspacePath, err := ValidateWorkspacePath(root, runtimeType, req)
	if err != nil {
		return "", err
	}
	if err := os.MkdirAll(root, 0o755); err != nil {
		return "", fmt.Errorf("create workspace root: %w", err)
	}
	runtimePath := filepath.Join(root, runtimeType)
	if err := os.MkdirAll(runtimePath, 0o755); err != nil {
		return "", fmt.Errorf("create runtime workspace root: %w", err)
	}
	if err := os.Chmod(runtimePath, 0o755); err != nil {
		return "", fmt.Errorf("chmod runtime workspace root: %w", err)
	}
	userPath := filepath.Join(runtimePath, "user-"+strconv.Itoa(req.UserID))
	if err := os.MkdirAll(userPath, 0o755); err != nil {
		return "", fmt.Errorf("create user workspace root: %w", err)
	}
	if err := os.Chmod(userPath, 0o755); err != nil {
		return "", fmt.Errorf("chmod user workspace root: %w", err)
	}
	if err := os.MkdirAll(workspacePath, 0o750); err != nil {
		return "", fmt.Errorf("create workspace: %w", err)
	}
	homePath := filepath.Join(workspacePath, "home")
	if err := os.MkdirAll(homePath, 0o750); err != nil {
		return "", fmt.Errorf("create workspace home: %w", err)
	}

	realRoot, err := filepath.EvalSymlinks(root)
	if err != nil {
		return "", fmt.Errorf("realpath workspace root: %w", err)
	}
	realWorkspace, err := filepath.EvalSymlinks(workspacePath)
	if err != nil {
		return "", fmt.Errorf("realpath workspace: %w", err)
	}
	if !pathWithin(realRoot, realWorkspace) {
		return "", fmt.Errorf("%w: realpath %s escaped %s", ErrWorkspacePath, realWorkspace, realRoot)
	}
	if err := ChownWorkspace(workspacePath, req.UID, req.GID); err != nil {
		return "", fmt.Errorf("chown workspace: %w", err)
	}
	if err := ChownWorkspace(homePath, req.UID, req.GID); err != nil {
		return "", fmt.Errorf("chown workspace home: %w", err)
	}
	return workspacePath, nil
}

func pathWithin(root, child string) bool {
	root = filepath.Clean(root)
	child = filepath.Clean(child)
	if root == child {
		return true
	}
	rel, err := filepath.Rel(root, child)
	if err != nil {
		return false
	}
	return rel != "." && rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator))
}
