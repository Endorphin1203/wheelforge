package com.wheelforge.api.security;

public interface AdminBootstrapCoordinator {
  void runWithInitializationLock(Runnable initialization);
}
