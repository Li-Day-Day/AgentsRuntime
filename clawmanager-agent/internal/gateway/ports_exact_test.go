package gateway

import (
	"errors"
	"strings"
	"testing"
)

func TestPortAllocatorReservesExactPortAndReleasesItsBlock(t *testing.T) {
	alloc := NewPortAllocator(func(int) bool { return false })
	alloc.SetBlockSize(3)

	port, err := alloc.ReserveExact(101, 7, 20003)
	if err != nil || port != 20003 {
		t.Fatalf("ReserveExact() = %d, %v; want 20003, nil", port, err)
	}
	if _, err := alloc.ReserveExact(102, 7, 20004); !errors.Is(err, ErrNoFreePort) || !strings.Contains(err.Error(), "20004") {
		t.Fatalf("overlapping ReserveExact() error = %v, want diagnostic ErrNoFreePort", err)
	}

	alloc.Release(20003)
	port, err = alloc.ReserveExact(102, 7, 20003)
	if err != nil || port != 20003 {
		t.Fatalf("ReserveExact() after release = %d, %v; want 20003, nil", port, err)
	}
}
