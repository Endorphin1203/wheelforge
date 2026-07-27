package com.wheelforge.api.requirements;

import java.util.Optional;
import org.springframework.data.jpa.repository.JpaRepository;

public interface RequirementFileRepository extends JpaRepository<RequirementFileEntity, String> {
  Optional<RequirementFileEntity> findByIdAndUserId(String id, String userId);
}
