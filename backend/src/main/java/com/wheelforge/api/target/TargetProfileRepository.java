package com.wheelforge.api.target;

import java.util.List;
import org.springframework.data.jpa.repository.JpaRepository;

public interface TargetProfileRepository extends JpaRepository<TargetProfileEntity, String> {
  List<TargetProfileEntity> findAllByEnabledTrueOrderByOsAscArchitectureAscPythonVersionAsc();
}
