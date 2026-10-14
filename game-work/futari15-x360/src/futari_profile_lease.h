#pragma once

#include <cerrno>
#include <filesystem>
#include <stdexcept>
#include <string>

#include <fcntl.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <unistd.h>

namespace futari {

inline constexpr char kProfileLockName[] = ".futari-profile.lock";
inline constexpr char kPendingReplayImportName[] = ".futari-replay-import.pending";

struct ProfileRootSelection {
  std::filesystem::path path;
  bool create_missing = false;
};

inline ProfileRootSelection SelectProfileRoot(const std::string& configured_root,
                                               const char* package_root,
                                               const std::filesystem::path& default_root) {
  if (package_root != nullptr) {
    if (package_root[0] == '\0' || package_root[0] != '/') {
      throw std::runtime_error("FUTARI_PACKAGE_USER_DATA must be a nonempty absolute path");
    }
    if (configured_root != package_root) {
      throw std::runtime_error(
          "FUTARI_PACKAGE_USER_DATA must exactly match --user_data_root");
    }
    return {std::filesystem::path(package_root), false};
  }
  if (!configured_root.empty()) {
    std::filesystem::path path(configured_root);
    if (!path.is_absolute()) {
      throw std::runtime_error("user_data_root must be an absolute path");
    }
    return {path, false};
  }
  return {default_root, true};
}

inline int OpenProfileDirectory(const std::filesystem::path& root, bool create_missing) {
  if (!root.is_absolute()) {
    throw std::runtime_error("Profile root must be an absolute path");
  }
  if (root == root.root_path()) {
    throw std::runtime_error("Filesystem root cannot be used as a profile root");
  }
  int directory_fd = open("/", O_RDONLY | O_DIRECTORY | O_CLOEXEC);
  if (directory_fd < 0) throw std::runtime_error("Cannot open filesystem root");

  try {
    for (const auto& component : root.relative_path()) {
      if (component.empty() || component == "." || component == "..") {
        throw std::runtime_error("Profile root contains an unsafe path component");
      }
      const std::string name = component.string();
      int next_fd = openat(directory_fd, name.c_str(),
                           O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
      if (next_fd < 0 && errno == ENOENT && create_missing) {
        if (mkdirat(directory_fd, name.c_str(), 0700) != 0 && errno != EEXIST) {
          throw std::runtime_error("Cannot create profile directory: " + name);
        }
        next_fd = openat(directory_fd, name.c_str(),
                         O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
      }
      if (next_fd < 0) {
        throw std::runtime_error("Profile root is missing, unsafe, or not a directory: " +
                                 root.string());
      }
      close(directory_fd);
      directory_fd = next_fd;
    }
    return directory_fd;
  } catch (...) {
    close(directory_fd);
    throw;
  }
}

inline void RefusePendingReplayImport(int directory_fd) {
  struct stat st {};
  if (fstatat(directory_fd, kPendingReplayImportName, &st, AT_SYMLINK_NOFOLLOW) == 0) {
    throw std::runtime_error(
        "Replay import recovery is pending or unsafe; use the launcher to recover this profile");
  }
  if (errno != ENOENT) {
    throw std::runtime_error(
        "Cannot safely inspect replay import recovery state; refusing to open this profile");
  }
}

class ProfileLease {
 public:
  ProfileLease() = default;
  explicit ProfileLease(const std::filesystem::path& root, bool create_missing = false) {
    Acquire(root, create_missing);
  }
  ~ProfileLease() { Reset(); }

  ProfileLease(ProfileLease&& other) noexcept : fd_(other.fd_) { other.fd_ = -1; }
  ProfileLease& operator=(ProfileLease&& other) noexcept {
    if (this != &other) {
      Reset();
      fd_ = other.fd_;
      other.fd_ = -1;
    }
    return *this;
  }
  ProfileLease(const ProfileLease&) = delete;
  ProfileLease& operator=(const ProfileLease&) = delete;

  void Acquire(const std::filesystem::path& root, bool create_missing = false) {
    if (fd_ >= 0) throw std::runtime_error("Profile lease is already held");
    int directory_fd = OpenProfileDirectory(root, create_missing);
    int fd = openat(directory_fd, kProfileLockName,
                    O_RDWR | O_CREAT | O_NOFOLLOW | O_CLOEXEC, 0600);
    if (fd < 0) {
      close(directory_fd);
      throw std::runtime_error("Cannot safely open profile lock: " + root.string());
    }
    struct stat st {};
    if (fstat(fd, &st) != 0 || !S_ISREG(st.st_mode)) {
      close(fd);
      close(directory_fd);
      throw std::runtime_error("Profile lock must be a regular file");
    }
    if (flock(fd, LOCK_EX | LOCK_NB) != 0) {
      close(fd);
      close(directory_fd);
      throw std::runtime_error("Profile is already in use");
    }
    try {
      RefusePendingReplayImport(directory_fd);
    } catch (...) {
      close(fd);
      close(directory_fd);
      throw;
    }
    close(directory_fd);
    fd_ = fd;
  }

  bool held() const { return fd_ >= 0; }

 private:
  void Reset() noexcept {
    if (fd_ >= 0) {
      close(fd_);
      fd_ = -1;
    }
  }

  int fd_ = -1;
};

}  // namespace futari
