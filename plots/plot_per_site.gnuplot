set terminal pdfcairo enhanced font "Helvetica,12" size 8,5
set output "per_site_doh_vs_odoh.pdf"

set xlabel "Website (Rank)"
set ylabel "Time (ms)"
set title "Per-Site DoH vs ODoH Comparison"
set key top right
set grid
set xtics rotate by -30

set style fill transparent solid 0.15
# set bars small

plot "cdf_doh_per_site.dat" using ($0):5:6:xtic(sprintf("%s (%d)", stringcolumn(2), $1)) with yerrorbars pt 0 ps 0 lw 2 dt 1 lc rgb "#0072B2" title "DoH Page Load", \
     "cdf_doh_per_site.dat" using ($0):5 with lines lw 2 dt 1 lc rgb "#0072B2" notitle, \
     "cdf_doh_per_site.dat" using ($0):3:4 with yerrorbars pt 0 ps 0 lw 2 dt 3 lc rgb "#0072B2" title "DoH DNS", \
     "cdf_doh_per_site.dat" using ($0):3 with lines lw 2 dt 3 lc rgb "#0072B2" notitle, \
     "cdf_odoh_per_site.dat" using ($0):5:6 with yerrorbars pt 0 ps 0 lw 2 dt 1 lc rgb "#D55E00" title "ODoH Page Load", \
     "cdf_odoh_per_site.dat" using ($0):5 with lines lw 2 dt 1 lc rgb "#D55E00" notitle, \
     "cdf_odoh_per_site.dat" using ($0):3:4 with yerrorbars pt 0 ps 0 lw 2 dt 3 lc rgb "#D55E00" title "ODoH DNS", \
     "cdf_odoh_per_site.dat" using ($0):3 with lines lw 2 dt 3 lc rgb "#D55E00" notitle
