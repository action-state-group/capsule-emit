package mysql

import (
	"context"
	"crypto/ed25519"
	"database/sql"
	"errors"
	"testing"

	"github.com/action-state-group/capsule-emit/go/artifact"
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

// TestMalformedIDBeforeIO: a malformed ID is invalid without reaching a server.
func TestMalformedIDBeforeIO(t *testing.T) {
	db, err := sql.Open("mysql", "root@tcp(127.0.0.1:1)/capsule_storage_test")
	require.NoError(t, err)
	require.NoError(t, db.Close())
	store, err := New(db, "test-closed", []ed25519.PublicKey{make([]byte, ed25519.PublicKeySize)})
	require.NoError(t, err)
	_, err = store.Get(t.Context(), "not-an-id")
	require.ErrorIs(t, err, artifact.ErrInvalid)
	require.ErrorIs(t, store.Purge(t.Context(), "not-an-id"), artifact.ErrInvalid)
}
