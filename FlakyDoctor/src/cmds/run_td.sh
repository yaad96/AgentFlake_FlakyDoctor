projectDir=$1
module=$2
test=$3
jdk=$4
forcing=$5

mainDir=${projectDir}
curDir=$(pwd)

## TD (Test/Timing-Dependent) reproduction/verification. Unlike OD (needs a polluter run first)
## or ID (needs NonDex), a TD victim fails deterministically only under the FlakyCodeChange timing
## forcing (${forcing}). We apply it on top of whatever fix is on disk, run the single victim once,
## then revert it -- so the victim runs on (fix + forcing) at verify time. If the forcing does not
## apply (the fix under repair overlaps the forced region) we do NOT run: a bare victim passes on
## its own and would be wrongly scored as fixed, so we record a failure instead (fail-closed).
## Stock Surefire is enough -- no testorder fork (unlike OD) and no NonDex (unlike ID). ${test} is
## the plain victim Class#method, the only repair target.
run_test(){
    echo mvn test -pl ${module} -Dtest=${test} -Drat.skip -Dcheckstyle.skip -Denforcer.skip=true -Dspotbugs.skip -Dmaven.test.failure.ignore=true -Djacoco.skip -Danimal.sniffer.skip -Dmaven.antrun.skip -Dspotless.check.skip
    mvn test -pl ${module} -Dtest=${test} -Drat.skip -Dcheckstyle.skip -Denforcer.skip=true -Dspotbugs.skip -Dmaven.test.failure.ignore=true -Djacoco.skip -Danimal.sniffer.skip -Dmaven.antrun.skip -Dspotless.check.skip
}

echo "* RUNNING TD victim ${test} STARTING at $(date)"
echo "* REPO VERSION $(git rev-parse HEAD)"

cd ${mainDir}
echo "* CURRENT DIR $(pwd)"
echo "* Expected Java version ${jdk}"

if  [[ ${jdk} == "8" ]]; then
    echo "Java version 8"
    if [[ -x /usr/libexec/java_home ]]; then
        export JAVA_HOME=$(/usr/libexec/java_home -v 1.8)
    else
        export JAVA_HOME=/usr/lib/jvm/java-1.8.0-openjdk-amd64
    fi
    export PATH=$JAVA_HOME/bin:$PATH
fi

if  [[ ${jdk} == "11" ]]; then
    echo "Java version 11"
    if [[ -x /usr/libexec/java_home ]]; then
        export JAVA_HOME=$(/usr/libexec/java_home -v 11)
    else
        export JAVA_HOME=/usr/lib/jvm/java-1.11.0-openjdk-amd64
    fi
    export PATH=$JAVA_HOME/bin:$PATH
fi

echo CURRENT DIR $(pwd)

## Apply the FlakyCodeChange timing forcing, run the victim, then revert it. If it does not apply
## cleanly (the fix overlaps the forced region) do NOT run -- record a failure (fail-closed).
if patch -p1 -F0 --no-backup-if-mismatch -s --dry-run -i ${forcing} >/dev/null 2>&1; then
    patch -p1 -F0 --no-backup-if-mismatch -s -i ${forcing}
    run_test ${test}
    patch -p1 -R -F0 --no-backup-if-mismatch -s -i ${forcing} >/dev/null 2>&1
else
    echo "* FlakyCodeChange forcing did not apply (fix overlaps the forced region) -- recording a failure (fail-closed)"
    echo "BUILD SUCCESS"
    echo "Tests run: 1, Failures: 1, Errors: 0, Skipped: 0"
fi

cd ${curDir}
echo "* RUNNING TD victim ${test} ENDING at $(date)"
