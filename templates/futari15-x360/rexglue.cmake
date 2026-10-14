# Generated guest code and all build outputs remain in the external build root.
set(REXSDK_DIR "" CACHE PATH "Path to the pinned ReXGlue SDK source tree")
if(NOT REXSDK_DIR)
  message(FATAL_ERROR "Set REXSDK_DIR to the SDK in the external build root")
endif()
add_subdirectory("${REXSDK_DIR}" rexglue-sdk)

if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/generated/default/sources.cmake")
  include(generated/default/sources.cmake)
  set(REXGLUE_ENTRYPOINT_GENERATED_SOURCES ${GENERATED_SOURCES})
  set(REXGLUE_ENTRYPOINT_INCLUDE_DIR "${CMAKE_CURRENT_SOURCE_DIR}/generated/default")
endif()

set(REXGLUE_RECOMP_DEBUG_INFO "line-tables-only" CACHE STRING
  "Debug info level for generated code: line-tables-only, full, or none")
set(REXGLUE_RECOMP_OPTIONS "")
if(REXGLUE_RECOMP_DEBUG_INFO STREQUAL "none")
  list(APPEND REXGLUE_RECOMP_OPTIONS $<$<CXX_COMPILER_ID:Clang,AppleClang,GNU>:-g0>)
elseif(REXGLUE_RECOMP_DEBUG_INFO STREQUAL "line-tables-only")
  list(APPEND REXGLUE_RECOMP_OPTIONS $<$<CXX_COMPILER_ID:Clang,AppleClang>:-gline-tables-only>)
endif()
if(REXGLUE_ENTRYPOINT_GENERATED_SOURCES)
  set_source_files_properties(${REXGLUE_ENTRYPOINT_GENERATED_SOURCES}
    PROPERTIES COMPILE_OPTIONS "${REXGLUE_RECOMP_OPTIONS}")
endif()

function(rexglue_apply_recomp_settings target_name pch_header)
  target_precompile_headers(${target_name} PRIVATE "${pch_header}")
  target_compile_options(${target_name} PRIVATE ${REXGLUE_RECOMP_OPTIONS})
endfunction()

macro(rexglue_setup_target target_name)
  if(REXGLUE_ENTRYPOINT_GENERATED_SOURCES)
    add_library(${target_name}_recomp OBJECT ${REXGLUE_ENTRYPOINT_GENERATED_SOURCES})
    target_include_directories(${target_name}_recomp PRIVATE
      "${CMAKE_CURRENT_SOURCE_DIR}" "${CMAKE_CURRENT_SOURCE_DIR}/src"
      "${REXGLUE_ENTRYPOINT_INCLUDE_DIR}")
    target_link_libraries(${target_name}_recomp PRIVATE rex::runtime)
    rexglue_apply_target_settings(${target_name}_recomp)
    rexglue_apply_recomp_settings(${target_name}_recomp
      "${REXGLUE_ENTRYPOINT_INCLUDE_DIR}/futari15x360_pch.h")
    target_link_libraries(${target_name} PRIVATE ${target_name}_recomp)
    add_dependencies(${target_name}_recomp ${target_name}_codegen)
  endif()
  add_dependencies(${target_name} ${target_name}_codegen)
  target_include_directories(${target_name} PRIVATE
    "${CMAKE_CURRENT_SOURCE_DIR}" "${CMAKE_CURRENT_SOURCE_DIR}/src"
    "${CMAKE_CURRENT_SOURCE_DIR}/generated/default")
  target_link_libraries(${target_name} PRIVATE rex::runtime)
  rexglue_configure_target(${target_name} ${ARGN})
endmacro()

add_custom_command(
  OUTPUT "${CMAKE_CURRENT_SOURCE_DIR}/generated/default/codegen.build.stamp"
  DEPFILE "${CMAKE_CURRENT_SOURCE_DIR}/generated/default/codegen.d"
  COMMAND $<TARGET_FILE:rex::rexglue> codegen "${CMAKE_CURRENT_SOURCE_DIR}/futari15x360_manifest.toml"
  DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/futari15x360_manifest.toml"
  WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}"
  COMMENT "Generating guest code from the supplied game files"
  VERBATIM)
add_custom_target(futari15x360_codegen
  DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/generated/default/codegen.build.stamp")

set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS
  "${CMAKE_CURRENT_SOURCE_DIR}/futari15x360_manifest.toml"
  "${CMAKE_CURRENT_SOURCE_DIR}/generated/default/dll_targets.cmake"
  "${CMAKE_CURRENT_SOURCE_DIR}/generated/default/sources.cmake")

if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/generated/default/dll_targets.cmake")
  include(generated/default/dll_targets.cmake)
endif()
