// futari15x360 - ReXGlue Recompiled Project
//
// Customize your app by overriding virtual hooks from rex::ReXApp.

#pragma once

#include <cstdlib>
#include <cstdio>
#include <filesystem>
#include <stdexcept>
#include <string>

#include <rex/cvar.h>
#include <rex/filesystem.h>
#include <rex/rex_app.h>
#include "futari_config_paths.h"
#include "futari_profile_lease.h"

class Futari15x360App : public rex::ReXApp {
 public:
  using rex::ReXApp::ReXApp;

  static std::unique_ptr<rex::ui::WindowedApp> Create(
      rex::ui::WindowedAppContext& ctx) {
    return std::unique_ptr<Futari15x360App>(new Futari15x360App(ctx, "futari15x360",
        PPCImageConfig));
  }

  bool OnInitialize() override {
    try {
      const std::string configured_root = rex::cvar::GetFlagByName("user_data_root");
      const char* package_root = std::getenv("FUTARI_PACKAGE_USER_DATA");
      // Match SetupEnvironment's default exactly, before it reads config or
      // constructs the runtime. A missing default profile is created safely.
      const auto selection = futari::SelectProfileRoot(
          configured_root, package_root, rex::filesystem::GetUserFolder() / GetName());
      profile_lease_.Acquire(selection.path, selection.create_missing);
    } catch (const std::exception& e) {
      std::fprintf(stderr, "Futari profile lock refused startup: %s\n", e.what());
      return false;
    }
    return rex::ReXApp::OnInitialize();
  }

  // Override virtual hooks for customization:
  // void OnPostInitLogging() override {}
  // void OnPreSetup(rex::RuntimeConfig& config) override {}
  // void OnLoadXexImage(std::string& xex_image) override {}
  // void OnPostLoadXexImage() override {}
  // void OnPostSetup() override {}
  // void OnCreateDialogs(rex::ui::ImGuiDrawer* drawer) override {}
  // std::unique_ptr<rex::ui::ImGuiDialog> CreateAchievementsOverlay() override;
  // std::unique_ptr<rex::ui::AchievementNotificationDialog>
  // CreateAchievementNotificationDialog() override;
  // void OnShutdown() override {}
  void OnConfigurePaths(rex::PathConfig& paths) override {
    // Keep settings with the selected profile, never beside the executable in
    // the signed .app. Existing legacy bundle settings are intentionally left
    // in place; they are not copied over an existing profile config.
    paths.config_path = futari::ConfigPathForProfile(paths.user_data_root,
                                                      paths.config_path);
  }

 private:
  futari::ProfileLease profile_lease_;
};
