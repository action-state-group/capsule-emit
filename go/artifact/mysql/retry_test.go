package mysql

import (
	"context"
	"errors"
	"testing"

	driver "github.com/go-sql-driver/mysql"
	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestDeadlockRetriesAreBounded(t *testing.T) {
	deadlock := &driver.MySQLError{Number: 1213, Message: "test deadlock"}
	count := 0
	err := retryDeadlock(t.Context(), func() error {
		count++
		if count < 3 {
			return deadlock
		}
		return nil
	})
	require.NoError(t, err)
	assert.Equal(t, 3, count)
	count = 0
	err = retryDeadlock(t.Context(), func() error { count++; return deadlock })
	require.ErrorIs(t, err, deadlock)
	assert.Equal(t, 5, count)
	permanent := errors.New("not a deadlock")
	count = 0
	err = retryDeadlock(t.Context(), func() error { count++; return permanent })
	require.ErrorIs(t, err, permanent)
	assert.Equal(t, 1, count)
}

func TestDeadlockCancellation(t *testing.T) {
	ctx, cancel := context.WithCancel(t.Context())
	err := retryDeadlock(ctx, func() error { cancel(); return &driver.MySQLError{Number: 1213} })
	require.ErrorIs(t, err, context.Canceled)
	called := false
	err = retryDeadlock(ctx, func() error { called = true; return nil })
	require.ErrorIs(t, err, context.Canceled)
	assert.False(t, called)
}
