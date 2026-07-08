projectDir=$1
module=$2
wrapperTest=$3
jdk=$4

mainDir=${projectDir}
curDir=$(pwd)

## NIO (Non-Idempotent-Outcome) reproduction. ${wrapperTest} is the auto-generated
## wrapper (WrapperClass#runTwice) that invokes the victim twice in the SAME JVM (full
## JUnit lifecycle each time) and asserts both invocations pass. A non-idempotent victim
## passes the first run and fails the second, so the wrapper test fails. Stock Surefire is
## enough — no testorder fork is needed (unlike OD).
run_test(){
    echo mvn test -pl ${module} -Dtest=${wrapperTest} -Drat.skip -Dcheckstyle.skip -Denforcer.skip=true -Dspotbugs.skip -Dmaven.test.failure.ignore=true -Djacoco.skip -Danimal.sniffer.skip -Dmaven.antrun.skip -Dspotless.check.skip
    mvn test -pl ${module} -Dtest=${wrapperTest} -Drat.skip -Dcheckstyle.skip -Denforcer.skip=true -Dspotbugs.skip -Dmaven.test.failure.ignore=true -Djacoco.skip -Danimal.sniffer.skip -Dmaven.antrun.skip -Dspotless.check.skip
}

echo "* RUNNING NIO wrapper ${wrapperTest} STARTING at $(date)"
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

run_test ${wrapperTest}

cd ${curDir}
echo "* RUNNING NIO wrapper ${wrapperTest} ENDING at $(date)"
