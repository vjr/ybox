#!/bin/bash

PYLINT=
if [ "$1" = "-l" ]; then
  echo -e '\033[35mWill also run pylint on the code\033[00m'
  echo
  PYLINT=1
fi

export MYPYPATH=./src
for f in src/zbox/*.py src/zbox/pkg/*.py src/zbox-* arch/pkg*; do
  echo -------------------------------------------
  echo Output of mypy on $f
  echo -------------------------------------------
  mypy $f
done

if [ -n "$PYLINT" ]; then
  export PYTHONPATH=./src
  for f in src/zbox/*.py src/zbox/pkg/*.py; do
    echo -------------------------------------------
    echo -------------------------------------------
    echo
    echo Output of pylint on $f
    echo
    echo -------------------------------------------
    echo -------------------------------------------
    pylint $f
  done
  for f in src/zbox-* arch/pkg*; do
    echo -------------------------------------------
    echo -------------------------------------------
    echo
    echo Output of pylint on $f
    echo
    echo -------------------------------------------
    echo -------------------------------------------
    pylint --module-rgx='[a-z][a-z0-9\-]*[a-z0-9]' $f
  done
fi
