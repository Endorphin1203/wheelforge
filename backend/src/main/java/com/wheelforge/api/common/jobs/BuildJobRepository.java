package com.wheelforge.api.common.jobs;

import java.util.Optional;
import org.springframework.data.jpa.repository.JpaRepository;

public interface BuildJobRepository extends JpaRepository<BuildJobEntity, String> {
  Optional<BuildJobEntity> findBySubjectIdAndJobType(String subjectId, String jobType);
}
