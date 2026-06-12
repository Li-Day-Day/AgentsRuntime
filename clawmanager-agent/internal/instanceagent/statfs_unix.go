//go:build linux || darwin || freebsd || openbsd || netbsd

package instanceagent

import "syscall"

func diskStats(path string) (total int64, free int64) {
	var stat syscall.Statfs_t
	if err := syscall.Statfs(path, &stat); err != nil {
		return 0, 0
	}
	return int64(stat.Blocks) * int64(stat.Bsize), int64(stat.Bavail) * int64(stat.Bsize)
}
