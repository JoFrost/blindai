#!/usr/bin/env just --justfile

default:
  @just --list

# Run on SGX hardware
run *args:
  cargo run {{args}}

# Build for SGX target
build *args:
  cargo build {{args}}

# Check for SGX target
check *args:
  cargo check {{args}}

# Build for a Linux target (no SGX)
build-no-sgx *args:
  cargo build --target=x86_64-unknown-linux-gnu {{args}}

# Run on a Linux target (no SGX)
run-no-sgx *args:
  cargo run --target=x86_64-unknown-linux-gnu {{args}}

# Run in the simulator
run-simu *args:
  #!/usr/bin/env bash
  set -e

  cargo build {{args}}

  binpath=`cargo build {{args}} --message-format json 2>/dev/null \
    | jq -r 'select(.reason=="compiler-artifact" and .target.kind==["bin"]) | .executable'` 
  
  ftxsgx-elf2sgxs "$binpath" \
    --heap-size 0xFBA00000 \
    --ssaframesize 1 \
    --stack-size 0x40000 \
    --threads 20

  ftxsgx-simulator "$binpath.sgxs"

# Execute with valgrind instrumentation
valgrind *args:
  #!/usr/bin/env bash
  set -e

  cargo build {{args}}

  binpath=`cargo build {{args}} --message-format json 2>/dev/null \
    | jq -r 'select(.reason=="compiler-artifact" and .target.kind==["bin"]) | .executable'`

  ftxsgx-elf2sgxs "$binpath" \
    --heap-size 0x2000000 \
    --ssaframesize 1 \
    --stack-size 0x20000 \
    --threads 20

  valgrind --sigill-diagnostics=no --leak-check=no ftxsgx-simulator "$binpath.sgxs" 

# Build and serve locally the documentation
doc:
  #!/usr/bin/env bash
  cd client \
  && poetry install --no-root \
  && poetry run pip install -r ../docs/requirements.txt \
  && poetry run bash -c 'cd .. && . docs/generate_api_reference.sh' \
  && poetry run mkdocs serve -f ../mkdocs.yml

# Run all tests and display combined coverage (don't forget to generate the onnx and npz files before)
test:
  #!/usr/bin/env bash
  set -e 
  cd client
  poetry run coverage run -m pytest
  cargo run --release &
  sleep 15
  for d in ../tests/*/ ; do
    onnx_files=($d*.onnx)
    npz_files=($d*.npz)
    poetry run coverage run --append ../tests/assert_correctness.py "${onnx_files[0]}" "${npz_files[0]}"
  done
  killall ftxsgx-runner
  coverage html --include=client/client.py,client/utils.py -d coverage_html
  poetry run python -m http.server 8000 --directory coverage_html/