#pragma once
// Declarations + fault control shared between the mechanically-renamed
// copy of content_manager.cpp and the production-path install test.
//
// scripts/test_content_install.py copies the REAL
// tools/rexglue-sdk/src/system/xam/content_manager.cpp into a temp dir
// and applies exactly three mechanical substitutions:
//   fwrite(               -> rextest_fwrite(
//   fclose(               -> rextest_fclose(
//   kernel_state_->title_id() -> rextest_title_id()
// then compiles the copy with this header force-included
// (-include). The definitions live in test_content_install.cpp.
//
// No protected SDK file is modified; the substitutions are recorded and
// counted by the driver, and the original file's hash is verified.

#include <cstddef>
#include <cstdint>
#include <cstdio>

extern "C" size_t rextest_fwrite(const void* ptr, size_t size, size_t count, std::FILE* f);
extern "C" int rextest_fclose(std::FILE* f);
extern "C" uint32_t rextest_title_id();

namespace rextest {

// Deterministic fault injection, driven by the test TU.
struct FaultControl {
  // Allowed successful fwrite calls before short writes begin
  // (SIZE_MAX = never fail).
  size_t fwrite_ok_left = static_cast<size_t>(-1);
  // Allowed successful fclose calls (0 = the next fclose reports failure).
  size_t fclose_ok_left = static_cast<size_t>(-1);
  // Fail ONLY the .header write, identified by a single write of exactly
  // header_write_size bytes. Metadata lives outside the package directory
  // (Headers/<type>/), so the payload rollback cannot repair it and a test
  // has to be able to target it specifically (Astra review 2026-09-22).
  bool fail_header_write = false;
  size_t header_write_size = 0;
  // Observability counters.
  uint64_t fwrite_calls = 0;
  uint64_t fclose_calls = 0;
  uint64_t bytes_written = 0;
  uint64_t header_writes_failed = 0;
  // Last reported failure reason (for test assertions).
  enum class Fail { none, fwrite_short, fclose_failed, header_short }
      last_fail = Fail::none;
};

inline FaultControl& faults() {
  static FaultControl f;
  return f;
}

}  // namespace rextest
