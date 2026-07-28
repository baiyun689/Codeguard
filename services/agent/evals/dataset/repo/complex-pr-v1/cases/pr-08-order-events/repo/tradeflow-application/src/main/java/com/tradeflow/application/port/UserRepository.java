package com.tradeflow.application.port;

import com.tradeflow.domain.UserAccount;
import java.util.Optional;

public interface UserRepository {
    Optional<UserAccount> findById(String id);
    Optional<UserAccount> findByTenantAndId(String tenantId, String id);
    void save(UserAccount account);
}
