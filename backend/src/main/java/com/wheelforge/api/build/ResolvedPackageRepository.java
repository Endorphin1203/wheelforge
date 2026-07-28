package com.wheelforge.api.build;

import java.util.List;
import org.springframework.data.jpa.repository.JpaRepository;

public interface ResolvedPackageRepository extends JpaRepository<ResolvedPackageEntity, String> {
  List<ResolvedPackageEntity> findAllByBuildTaskIdOrderByNormalizedNameAsc(String buildTaskId);
}
