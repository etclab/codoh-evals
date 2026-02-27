set terminal pdfcairo enhanced font "Helvetica,12" size 6,4
set output "cdf_odoh_doh.pdf"

set xlabel "Time (ms)"
set ylabel "CDF"
set yrange [0:1]
set key right bottom
set grid

plot "cdf_odoh.dat" index 1 using 1:2 with linespoints pt 5 ps 0.5 lw 2 title "ODoH Wall-Clock DNS", \
     "cdf_odoh.dat" index 2 using 1:2 with linespoints pt 9 ps 0.5 lw 2 title "ODoH Page Load", \
     "cdf_doh.dat" index 1 using 1:2 with linespoints pt 4 ps 0.5 lw 2 title "DoH Wall-Clock DNS", \
     "cdf_doh.dat" index 2 using 1:2 with linespoints pt 8 ps 0.5 lw 2 title "DoH Page Load"
