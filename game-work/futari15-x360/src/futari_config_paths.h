// Futari app profile-specific config path.
//
// The SDK default points beside the executable, which is inside the signed
// application bundle for the packaged macOS app. Futari settings follow the
// resolved user-data root (explicit profile override or platform default).
// Preserve the SDK path only if a caller supplies no user-data root at all.
#pragma once

#include <filesystem>

namespace futari {

inline std::filesystem::path ConfigPathForProfile(
    const std::filesystem::path& user_data_root,
    const std::filesystem::path& default_config_path) {
  if (user_data_root.empty()) {
    return default_config_path;
  }
  return user_data_root / default_config_path.filename();
}

}  // namespace futari
