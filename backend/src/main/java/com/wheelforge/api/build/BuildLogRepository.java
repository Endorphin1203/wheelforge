package com.wheelforge.api.build;

import java.util.List;
import org.springframework.data.domain.Pageable;
import org.springframework.data.jpa.repository.JpaRepository;

public interface BuildLogRepository extends JpaRepository<BuildLogEntity, String> {
  List<BuildLogEntity> findByBuildTaskIdAndSequenceNoGreaterThanOrderBySequenceNoAsc(
      String buildTaskId, long sequenceNo, Pageable pageable);
}
