$PROBLEM    MOXONIDINE PK,FINAL ESTIMATES,ALL DATA
;;

;;
$INPUT      ID VISI XAT2=DROP DGRP=DROP DOSE=DROP FLAG=DROP ONO=DROP
            XIME=DROP NEUY SCR AGE SEX NYHA WT DROP ACE DIG
            DIU NUMB=DROP TAD TIME VIDD=DROP CRCL AMT SS II DROP
            CMT=DROP CONO=DROP DV EVID=DROP OVID=DROP DROP
;		ID,VISI,XAT2,DGRP,DOSE,FLAG,ONO,

;		XIME,DVO,NEUY,SCR,AGE,SEX,NYHA,WT,COMP,ACE,DIG,

;		DIU,NUMB,TAD,TIME,VIDD,CLCR,AMT,SS,II,VID,

;		CMT,CONO,DV,EVID,OVID
$DATA      ../../../../models/mox_simulated_log.csv IGNORE=@
$SUBROUTINE ADVAN2 TRANS1
$PK


;-----------OCCASIONS----------
   VIS3               = 0
   IF(VISI.EQ.3) VIS3 = 1
   VIS8               = 0
   IF(VISI.EQ.8) VIS8 = 1

;----------IOV--------------------

   KPCL  = VIS3*ETA(4)+VIS8*ETA(5)
   KPKA  = VIS3*ETA(6)+VIS8*ETA(7)

; --------------- Remove block structure to be able fo fix variances/covariances within a block ----------------------

; ---- IIV ---

; Diagonal (Variance)
OM11   = THETA(6)
OM22   = THETA(7)

; Off-diagonal (Correlation)
COR12  = THETA(8)

; Off-diagonal (Covariance)
COV12  = SQRT(OM11*OM22)*COR12

; Random effect 1
P1     = SQRT(OM11)*ETA(1)

; Random effect 2
DP12   = COV12/SQRT(OM11)    ; = SQRT(OM22)*COR12
P2     = DP12*ETA(1) + SQRT(OM22-(DP12*DP12))*ETA(2)

;---------- PK model ------------------

   TVCL  = THETA(1)
   TVV   = THETA(2)

   CL    = TVCL*EXP(P1)*EXP(KPCL)
   V     = TVV*EXP(P2)
   KA    = THETA(3)*EXP(ETA(3)+KPKA)
   ALAG1 = THETA(4)
   K     = CL/V
   S2    = V


$ERROR


IPRED = LOG(.025)
IF(F.GT.0) IPRED = LOG(F)

W     = THETA(5)
IRES  = IPRED-DV
IWRES = IRES/W

Y     = IPRED+EPS(1)*W

$THETA  (0,26.6815) ; CL
$THETA  (0,110.275) ; V
$THETA  (0,4.49465) ; KA
$THETA  (0,0.240122) ; TLAG
$THETA  (0,0.330604) ; RUV
$THETA  (0,0.330604) ; VAR CL
$THETA  (0,0.330604) ; VAR V
$THETA  (0,0.7,1) ; CORR VAR CL-V
$OMEGA  1  FIX  ;     IIV CL
$OMEGA  1  FIX  ;      IIV V
$OMEGA  2.81618  ;     VAR KA
$OMEGA  BLOCK(1)
 0.0147009  ; IOV CL OCC1
$OMEGA  BLOCK(1) SAME
$OMEGA  BLOCK(1)
 0.506018  ; IOV KA OCC1
$OMEGA  BLOCK(1) SAME
$SIGMA  1  FIX
$ESTIMATION METHOD=BAYES INTERACTION NBURN=100 NITER=100 CTYPE=3
            PRINT=100 CITER=10 CALPHA=0.05 NOPRIOR=0
$ESTIMATION METHOD=COND MAXEVALS=100
$COVARIANCE PRINT=E UNCOND
