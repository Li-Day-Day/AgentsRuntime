package instanceagent

import "runtime"

func goArch() string {
	return runtime.GOARCH
}
