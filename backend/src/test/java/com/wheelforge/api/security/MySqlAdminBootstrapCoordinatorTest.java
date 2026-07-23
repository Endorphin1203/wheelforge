package com.wheelforge.api.security;

import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.BDDMockito.given;
import static org.mockito.Mockito.doThrow;
import static org.mockito.Mockito.inOrder;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;

import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.util.concurrent.atomic.AtomicBoolean;
import org.junit.jupiter.api.Test;
import org.mockito.InOrder;
import org.mockito.Mockito;
import org.springframework.jdbc.core.ConnectionCallback;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.transaction.support.TransactionCallback;
import org.springframework.transaction.support.TransactionTemplate;

class MySqlAdminBootstrapCoordinatorTest {
  @Test
  void holdsTheMySqlLockAcrossInitializationAndReleasesIt() throws Exception {
    JdbcTemplate jdbcTemplate = Mockito.mock(JdbcTemplate.class);
    TransactionTemplate transactionTemplate = Mockito.mock(TransactionTemplate.class);
    Runnable initialization = Mockito.mock(Runnable.class);
    LockConnection lockConnection = lockConnection(jdbcTemplate, transactionTemplate, 1, 1);

    new MySqlAdminBootstrapCoordinator(jdbcTemplate, transactionTemplate)
        .runWithInitializationLock(initialization);

    InOrder order = inOrder(lockConnection.connection, transactionTemplate, initialization);
    order.verify(lockConnection.connection).prepareStatement("SELECT GET_LOCK(?, ?)");
    order.verify(transactionTemplate).execute(Mockito.any(TransactionCallback.class));
    order.verify(initialization).run();
    order.verify(lockConnection.connection).prepareStatement("SELECT RELEASE_LOCK(?)");
  }

  @Test
  void refusesToInitializeWhenTheMySqlLockTimesOut() throws Exception {
    JdbcTemplate jdbcTemplate = Mockito.mock(JdbcTemplate.class);
    TransactionTemplate transactionTemplate = Mockito.mock(TransactionTemplate.class);
    Runnable initialization = Mockito.mock(Runnable.class);
    LockConnection lockConnection = lockConnection(jdbcTemplate, transactionTemplate, 0, 1);

    assertThatThrownBy(
            () ->
                new MySqlAdminBootstrapCoordinator(jdbcTemplate, transactionTemplate)
                    .runWithInitializationLock(initialization))
        .isInstanceOf(IllegalStateException.class)
        .hasMessageContaining("Timed out acquiring");
    verify(initialization, never()).run();
    verify(lockConnection.connection, never()).prepareStatement("SELECT RELEASE_LOCK(?)");
  }

  @Test
  void releasesTheLockWhenInitializationFails() throws Exception {
    JdbcTemplate jdbcTemplate = Mockito.mock(JdbcTemplate.class);
    TransactionTemplate transactionTemplate = Mockito.mock(TransactionTemplate.class);
    Runnable initialization = Mockito.mock(Runnable.class);
    LockConnection lockConnection = lockConnection(jdbcTemplate, transactionTemplate, 1, 1);
    doThrow(new IllegalStateException("save failed")).when(initialization).run();

    assertThatThrownBy(
            () ->
                new MySqlAdminBootstrapCoordinator(jdbcTemplate, transactionTemplate)
                    .runWithInitializationLock(initialization))
        .isInstanceOf(IllegalStateException.class)
        .hasMessage("save failed");
    verify(lockConnection.connection).prepareStatement("SELECT RELEASE_LOCK(?)");
  }

  @SuppressWarnings({"unchecked", "rawtypes"})
  private LockConnection lockConnection(
      JdbcTemplate jdbcTemplate,
      TransactionTemplate transactionTemplate,
      int acquireResult,
      int releaseResult) {
    Connection connection = Mockito.mock(Connection.class);
    PreparedStatement acquire = Mockito.mock(PreparedStatement.class);
    PreparedStatement release = Mockito.mock(PreparedStatement.class);
    ResultSet acquireRows = Mockito.mock(ResultSet.class);
    ResultSet releaseRows = Mockito.mock(ResultSet.class);
    AtomicBoolean transactionFinished = new AtomicBoolean();
    try {
      given(jdbcTemplate.execute(Mockito.<ConnectionCallback<Void>>any()))
          .willAnswer(
              invocation ->
                  ((ConnectionCallback<Void>) invocation.getArgument(0))
                      .doInConnection(connection));
      given(connection.prepareStatement("SELECT GET_LOCK(?, ?)")).willReturn(acquire);
      given(connection.prepareStatement("SELECT RELEASE_LOCK(?)")).willReturn(release);
      given(acquire.executeQuery()).willReturn(acquireRows);
      given(release.executeQuery())
          .willAnswer(
              invocation -> {
                if (!transactionFinished.get()) {
                  throw new AssertionError(
                      "released the MySQL lock before the transaction completed");
                }
                return releaseRows;
              });
      given(acquireRows.next()).willReturn(true);
      given(releaseRows.next()).willReturn(true);
      given(acquireRows.getInt(1)).willReturn(acquireResult);
      given(releaseRows.getInt(1)).willReturn(releaseResult);
    } catch (Exception exception) {
      throw new AssertionError(exception);
    }
    given(transactionTemplate.execute(Mockito.any(TransactionCallback.class)))
        .willAnswer(
            invocation -> {
              try {
                return ((TransactionCallback) invocation.getArgument(0))
                    .doInTransaction(Mockito.mock());
              } finally {
                transactionFinished.set(true);
              }
            });
    return new LockConnection(connection);
  }

  private record LockConnection(Connection connection) {}
}
