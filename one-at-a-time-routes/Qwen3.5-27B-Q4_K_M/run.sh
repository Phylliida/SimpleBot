#!/usr/bin/env bash
set -euo pipefail


# If we're not already in Konsole, re-run this script inside Konsole (and keep it open).
if [[ -z "${IMRUNNING:-}" ]]; then
  export IMRUNNING=1
  exec konsole --noclose -e bash "$0" "$@"
fi

NIXPKGS_ALLOW_UNFREE=1 nix-shell -p cmake clang vulkan-tools vulkan-headers vulkan-loader cudaPackages.cuda_cccl cudaPackages.cuda_cudart cudaPackages.cuda_cupti cudaPackages.cuda_nvcc cudaPackages.cuda_nvml_dev cudaPackages.cuda_nvrtc cudaPackages.cuda_nvtx cudaPackages.libcublas cudaPackages.libcufft cudaPackages.libcufile cudaPackages.libcurand cudaPackages.libcusolver cudaPackages.libcusparse cudaPackages.cudatoolkit cudaPackages.nccl cudaPackages.cudnn cudaPackages.libcublas cudaPackages.cuda_nvcc cudaPackages.libcusparse cudaPackages.libcusolver cudaPackages.cuda_cudart cudaPackages.cuda_opencl cudaPackages.libcublas cudaPackages.cuda_nvcc cudaPackages.cuda_cudart cudaPackages.cuda_cccl ripgrep curl glslang shaderc libclang libusb1 eigen xorg.libX11 ninja pkg-config jsoncpp mesa libglvnd xorg.libX11 xorg.libX11.dev xorg.libXext xorg.libXext.dev xorg.libXrandr xorg.libXrandr.dev opencv gst_all_1.gstreamer gst_all_1.gst-plugins-base gst_all_1.gst-plugins-good gst_all_1.gst-plugins-bad gst_all_1.gst-plugins-ugly gst_all_1.gst-libav gst_all_1.gst-vaapi ffmpeg x264 pipewire libcap avahi libsysprof-capture sysprof pcre pcre2 nlohmann_json cli11 glib mount util-linux libnotify librsvg libselinux libsepol libarchive libtiff libdeflate hidapi openhmd SDL2 cjson onnxruntime libuvc lerc libseccomp xz busybox libwebp libxdmcp expat dav1d libunwind harfbuzzFull harfbuzz libxml2 pango cairo fribidi libthai libdatrie libdwarf elfutils orc wayland wayland-scanner wayland-utils doxygen libsurvive openvr opencomposite dbus libbsd libdrm ripgrep rustup ccache --run '/home/bepis/Documents/coding/llama.cpp/build/bin/llama-server -m /home/bepis/Downloads/Qwen3.5-27B-Q4_K_M.gguf --flash-attn on --ctx-size 60000 --n-gpu-layers 99 --host 0.0.0.0 --port 8052 -np 1'
