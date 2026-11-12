package postgres

import (
	"context"
	"errors"
	"testing"

	"github.com/jackc/pgx/v5/pgconn"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestConflictRetriesAreBounded(t *testing.T) {
	for _, code := range []string{"40001", "40P01"} {
		failure := &pgconn.PgError{Code: code}
		count := 0
		err := retryConflict(t.Context(), func() error {
			count++
			if count < 3 {
				return failure
			}
			return nil
		})
		require.NoError(t, err)
		assert.Equal(t, 3, count)
		count = 0
		err = retryConflict(t.Context(), func() error { count++; return failure })
		require.ErrorIs(t, err, failure)
		assert.Equal(t, 5, count)
	}
	other := errors.New("not retryable")
	count := 0
	require.ErrorIs(t, retryConflict(t.Context(), func() error { count++; return other }), other)
	assert.Equal(t, 1, count)
}

func TestRetryStopsWhenContextEnds(t *testing.T) {
	canceled, stop := context.WithCancel(t.Context())
	stop()
	require.ErrorIs(t, retryConflict(canceled, func() error { return nil }), canceled.Err())
}

func TestRebind(t *testing.T) {
	assert.Equal(t, "SELECT $1,$2 WHERE a=$3", rebind("SELECT ?,? WHERE a=?"))
	assert.Equal(t, "SELECT 1", rebind("SELECT 1"))
}
