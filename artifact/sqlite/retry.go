package sqlite

import (
	"context"
	"errors"

	modernsqlite "modernc.org/sqlite"
)

// busy reports SQLITE_BUSY / SQLITE_LOCKED, the transient contention codes a
// single-writer database surfaces when another connection holds the write lock.
func busy(err error) bool {
	var se *modernsqlite.Error
	if errors.As(err, &se) {
		switch se.Code() & 0xff {
		case 5, 6: // SQLITE_BUSY, SQLITE_LOCKED
			return true
		}
	}
	return false
}

// constraint reports SQLITE_CONSTRAINT, used to detect an idempotent duplicate
// primary-key insert. The only constraint that can fire on the capsule parent
// insert is its primary key, so a byte-identical retry is reconciled by Get.
func constraint(err error) bool {
	var se *modernsqlite.Error
	if errors.As(err, &se) {
		return se.Code()&0xff == 19 // SQLITE_CONSTRAINT
	}
	return false
}

// retryBusy bounds transient-contention retries. busy_timeout already blocks the
// driver, so this is a backstop, not the primary serialization mechanism.
func retryBusy(ctx context.Context, run func() error) error {
	var err error
	for attempt := 0; attempt < 10; attempt++ {
		if err = run(); !busy(err) {
			return err
		}
		if ctx.Err() != nil {
			return ctx.Err()
		}
	}
	return err
}
