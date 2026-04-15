RUNS=10 SITES=sampled-1000-of-2000-resolvable.csv ./run-doh.sh --coredns
RUNS=10 SITES=sampled-1000-of-2000-resolvable.csv ./run-odoh.sh --coredns

RUNS=10 SITES=sampled-1000-of-2000-resolvable.csv ./run-codoh.sh


RUNS=1 SITES=top-1k.csv ./run-doh.sh --coredns