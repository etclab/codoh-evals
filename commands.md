RUNS=10 SITES=data/top-10k-resolvable.csv ./run-doh.sh --coredns
RUNS=10 SITES=data/top-10k-resolvable.csv ./run-odoh.sh --coredns

RUNS=10 SITES=data/top-10k-resolvable.csv ./run-codoh.sh


RUNS=1 SITES=data/top-10k-resolvable.csv ./run-doh.sh --coredns