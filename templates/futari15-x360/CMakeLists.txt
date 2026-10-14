cmake_minimum_required(VERSION 3.25)
project(futari15x360 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

include(generated/rexglue.cmake)

add_executable(futari15x360 src/main.cpp)
rexglue_setup_target(futari15x360 GPU_PLUGINS xenos)
target_link_libraries(futari15x360 PRIVATE rex::ui imgui::imgui)
