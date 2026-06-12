//go:build !linux && !darwin && !freebsd && !openbsd && !netbsd

package instanceagent

func diskStats(path string) (total int64, free int64) {
	return 0, 0
}
