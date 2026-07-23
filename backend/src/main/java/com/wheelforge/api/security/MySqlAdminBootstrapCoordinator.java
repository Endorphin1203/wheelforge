package com.wheelforge.api.security;

import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.util.function.Supplier;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.jdbc.core.ConnectionCallback;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Component;
import org.springframework.transaction.support.TransactionTemplate;

@Component
public class MySqlAdminBootstrapCoordinator implements AdminBootstrapCoordinator {
  private static final String LOCK_NAME = "wheelforge.admin-bootstrap";
  private static final int LOCK_TIMEOUT_SECONDS = 10;

  private final Supplier<JdbcTemplate> jdbcTemplateSupplier;
  private final Supplier<TransactionTemplate> transactionTemplateSupplier;

  @Autowired
  public MySqlAdminBootstrapCoordinator(
      ObjectProvider<JdbcTemplate> jdbcTemplateProvider,
      ObjectProvider<TransactionTemplate> transactionTemplateProvider) {
    this(jdbcTemplateProvider::getIfAvailable, transactionTemplateProvider::getIfAvailable);
  }

  MySqlAdminBootstrapCoordinator(
      JdbcTemplate jdbcTemplate, TransactionTemplate transactionTemplate) {
    this(() -> jdbcTemplate, () -> transactionTemplate);
  }

  private MySqlAdminBootstrapCoordinator(
      Supplier<JdbcTemplate> jdbcTemplateSupplier,
      Supplier<TransactionTemplate> transactionTemplateSupplier) {
    this.jdbcTemplateSupplier = jdbcTemplateSupplier;
    this.transactionTemplateSupplier = transactionTemplateSupplier;
  }

  @Override
  public void runWithInitializationLock(Runnable initialization) {
    JdbcTemplate jdbcTemplate = jdbcTemplateSupplier.get();
    if (jdbcTemplate == null) {
      throw new IllegalStateException(
          "Bootstrap administrator initialization requires a MySQL connection");
    }
    TransactionTemplate transactionTemplate = transactionTemplateSupplier.get();
    if (transactionTemplate == null) {
      throw new IllegalStateException(
          "Bootstrap administrator initialization requires a MySQL transaction manager");
    }
    jdbcTemplate.execute(
        (ConnectionCallback<Void>)
            lockConnection -> {
              runWhileLocked(lockConnection, transactionTemplate, initialization);
              return null;
            });
  }

  private void runWhileLocked(
      Connection lockConnection, TransactionTemplate transactionTemplate, Runnable initialization) {
    Integer acquired = queryLock(lockConnection, "SELECT GET_LOCK(?, ?)", LOCK_TIMEOUT_SECONDS);
    if (Integer.valueOf(0).equals(acquired)) {
      throw new IllegalStateException("Timed out acquiring MySQL bootstrap administrator lock");
    }
    if (!Integer.valueOf(1).equals(acquired)) {
      throw new IllegalStateException("Could not acquire MySQL bootstrap administrator lock");
    }
    Throwable initializationFailure = null;
    try {
      transactionTemplate.execute(
          status -> {
            initialization.run();
            return null;
          });
    } catch (RuntimeException | Error exception) {
      initializationFailure = exception;
      throw exception;
    } finally {
      try {
        Integer released = queryLock(lockConnection, "SELECT RELEASE_LOCK(?)", null);
        if (!Integer.valueOf(1).equals(released)) {
          throw new IllegalStateException("Could not release MySQL bootstrap administrator lock");
        }
      } catch (RuntimeException exception) {
        invalidateLockConnection(lockConnection, exception);
        if (initializationFailure != null) {
          initializationFailure.addSuppressed(exception);
        } else {
          throw exception;
        }
      }
    }
  }

  private void invalidateLockConnection(
      Connection lockConnection, RuntimeException releaseFailure) {
    try {
      lockConnection.abort(Runnable::run);
    } catch (SQLException | RuntimeException abortFailure) {
      releaseFailure.addSuppressed(abortFailure);
    }
  }

  private Integer queryLock(Connection connection, String query, Integer timeoutSeconds) {
    try (PreparedStatement statement = connection.prepareStatement(query)) {
      statement.setString(1, LOCK_NAME);
      if (timeoutSeconds != null) {
        statement.setInt(2, timeoutSeconds);
      }
      try (ResultSet result = statement.executeQuery()) {
        if (!result.next()) {
          return null;
        }
        return result.getInt(1);
      }
    } catch (SQLException exception) {
      throw new IllegalStateException(
          "Could not execute MySQL bootstrap administrator lock query", exception);
    }
  }
}
