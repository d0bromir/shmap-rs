#!/usr/bin/env bash

rm -rf target/
make bench > bench.txt

rm -rf target/
make avx2_bench > bench_avx2.txt

rm -rf target/
make native_bench > bench_native.txt