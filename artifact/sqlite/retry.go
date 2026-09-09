package sqlite

import (
	"errors"

	modernsqlite "modernc.org/sqlite"
)

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
