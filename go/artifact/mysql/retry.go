package mysql

import (
	"context"
	"errors"
	"time"

	driver "github.com/go-sql-driver/mysql"
)

// Concurrent duplicate INSERTs may acquire shared unique-index locks before
// upgrading to the parent-row lock. InnoDB can choose either transaction as a
// deadlock victim. Retrying is safe only for our own, fully rolled-back SQL
// transaction; PutTx must never replay a caller's wider workflow transaction.
func retryDeadlock(ctx context.Context, run func() error) error {
	for attempt := 0; ; attempt++ {
		if err := ctx.Err(); err != nil {
			return err
		}
		err := run()
		var mysqlErr *driver.MySQLError
		if !errors.As(err, &mysqlErr) || mysqlErr.Number != 1213 || attempt == 4 {
			return err
		}
		timer := time.NewTimer(time.Duration(1<<attempt) * 10 * time.Millisecond)
		select {
		case <-ctx.Done():
			timer.Stop()
			return ctx.Err()
		case <-timer.C:
		}
	}
}
