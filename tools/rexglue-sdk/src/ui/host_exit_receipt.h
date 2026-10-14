#pragma once

#include <cerrno>
#include <filesystem>
#include <string>

#include <rex/platform.h>

#if REX_PLATFORM_WIN32
#include <fcntl.h>
#include <io.h>
#include <process.h>
#include <sys/stat.h>
#else
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>
#endif

namespace rex::ui::internal {

inline const char* HostTitleTerminationStatus(bool returned) {
  return returned ? "title termination returned"
                  : "kernel state unavailable; title termination not attempted";
}

inline int CurrentProcessId() {
#if REX_PLATFORM_WIN32
  return _getpid();
#else
  return static_cast<int>(getpid());
#endif
}

inline bool WriteHostExitReceipt(const std::filesystem::path& log_path,
                                 bool normal_requested_close,
                                 bool title_termination_returned) {
  // The packaged launcher supplies a unique absolute log path beneath the
  // selected profile. Refuse relative paths so a malformed setting cannot
  // create a receipt in an arbitrary current directory.
  if (log_path.empty() || !log_path.is_absolute()) {
    return false;
  }

  std::filesystem::path receipt_path = log_path;
  receipt_path += ".title-termination-receipt.json";
  const std::string receipt =
      std::string("{\"schema_version\":1,\"process_id\":") +
      std::to_string(CurrentProcessId()) +
      ",\"normal_requested_close\":" + (normal_requested_close ? "true" : "false") +
      ",\"title_termination_returned\":" +
      (title_termination_returned ? "true" : "false") +
      ",\"log_flush_attempted\":true,\"hard_exit\":true,\"clean_exit\":false}\n";

#if REX_PLATFORM_WIN32
  int fd = _wopen(receipt_path.c_str(), _O_WRONLY | _O_CREAT | _O_EXCL | _O_BINARY,
                  _S_IREAD | _S_IWRITE);
  if (fd < 0) {
    return false;
  }
  size_t offset = 0;
  bool ok = true;
  while (offset < receipt.size()) {
    unsigned int chunk = static_cast<unsigned int>(receipt.size() - offset);
    int written = _write(fd, receipt.data() + offset, chunk);
    if (written <= 0) {
      ok = false;
      break;
    }
    offset += static_cast<size_t>(written);
  }
  if (_commit(fd) != 0) {
    ok = false;
  }
  if (_close(fd) != 0) {
    ok = false;
  }
  return ok;
#else
  int file_flags = O_WRONLY | O_CREAT | O_EXCL;
#ifdef O_NOFOLLOW
  file_flags |= O_NOFOLLOW;
#endif
  int fd = open(receipt_path.c_str(), file_flags, S_IRUSR | S_IWUSR);
  if (fd < 0) {
    return false;
  }

  size_t offset = 0;
  bool ok = true;
  while (offset < receipt.size()) {
    ssize_t written = write(fd, receipt.data() + offset, receipt.size() - offset);
    if (written < 0 && errno == EINTR) {
      continue;
    }
    if (written <= 0) {
      ok = false;
      break;
    }
    offset += static_cast<size_t>(written);
  }
  if (fsync(fd) != 0) {
    ok = false;
  }
  if (close(fd) != 0) {
    ok = false;
  }

  std::filesystem::path parent = receipt_path.parent_path();
  int dir_flags = O_RDONLY;
#ifdef O_NOFOLLOW
  dir_flags |= O_NOFOLLOW;
#endif
  int dir_fd = open(parent.c_str(), dir_flags);
  if (dir_fd < 0) {
    ok = false;
  } else {
    if (fsync(dir_fd) != 0) {
      ok = false;
    }
    close(dir_fd);
  }
  return ok;
#endif
}

}  // namespace rex::ui::internal
