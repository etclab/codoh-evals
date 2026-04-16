set terminal pdfcairo size 22,40 enhanced font 'Helvetica,18'
set output 'site_counts.pdf'

set grid ytics
set style fill solid 1.0 noborder
set boxwidth 0.9
unset xtics
set yrange [0:*]

set multiplot layout 10,1 title "Site counts per domain (top 1000, grouped by 100)" font 'Helvetica,22'

do for [i=0:9] {
    lo = i*100 + 1
    hi = (i+1)*100
    set title sprintf("Domains %d - %d", lo, hi)
    set xlabel "Domain index"
    set ylabel "site\\_count"
    set xrange [lo-0.5:hi+0.5]
    plot 'site_counts.dat' using 1:2 every ::lo-1::hi-1 with boxes lc rgb "#1f77b4" notitle, \
         'site_counts.dat' using 1:($2):3 every ::lo-1::hi-1 with labels font 'Helvetica,18' offset 0,1.8 rotate by 45 notitle
}

unset multiplot
