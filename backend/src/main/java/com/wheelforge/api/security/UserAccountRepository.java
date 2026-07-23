package com.wheelforge.api.security;

import java.util.Optional;
import org.springframework.data.jpa.repository.JpaRepository;

public interface UserAccountRepository extends JpaRepository<UserAccount, String> {
  Optional<UserAccount> findByUsername(String username);
}
