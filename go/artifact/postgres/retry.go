package postgres

import (
	"context"
	"errors"
	"time"

	"github.com/jackc/pgx/v5/pgconn"
)

// Two concurrent first writes of the same record, or a Put racing a Purge, can
// make PostgreSQL abort one transaction with a serialization failure (40001) or
// a deadlock (40P01). Retrying is safe only for our own, fully rolled-back SQL
// transaction; PutTx must never replay a caller's wider workflow transaction.
func retryConflict(ctx context.Context, run func() error) error {
	for attempt := 0; ; attempt++ {
		if err := ctx.Err(); err != nil {
			return err
		}
		err := run()
		if !retryable(err) || attempt == 4 {
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

func retryable(err error) bool {
	var pgErr *pgconn.PgError
	return errors.As(err, &pgErr) && (pgErr.Code == "40001" || pgErr.Code == "40P01")
}
