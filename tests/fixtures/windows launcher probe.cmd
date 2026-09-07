@echo off
setlocal EnableExtensions EnableDelayedExpansion
if not [%1] == [-mode] (
  echo RAW_ARG1_MISMATCH=[%1] 1>&2
  exit /b 91
)
if not [%2] == [tcl] (
  echo RAW_ARG2_MISMATCH=[%2] 1>&2
  exit /b 92
)
echo RAW1=[%1]
echo RAW2=[%2]
echo ARG3=[%~3]
echo ARG4=[%~4]
echo PROBE_STDERR 1>&2
exit /b 0
