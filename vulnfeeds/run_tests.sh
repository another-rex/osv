#!/bin/bash

set -e

go vet ./...
go test ./...