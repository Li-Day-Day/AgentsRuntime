//go:build !windows

package gateway

import "os"

func ChownWorkspace(path string, uid, gid int) error {
	if uid <= 0 || gid <= 0 {
		return nil
	}
	return os.Chown(path, uid, gid)
}
