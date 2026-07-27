package com.wheelforge.api.common.jobs;

import org.springframework.data.jpa.repository.JpaRepository;

public interface BuildJobRepository extends JpaRepository<BuildJobEntity, String> {}
